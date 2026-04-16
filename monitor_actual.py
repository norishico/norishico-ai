"""monitor_actual.py - v6.6 read-only actual vs BT comparison.

Aggregates monthly_results_*.json against baselines parsed from
docs/OPERATIONAL_MONITORING.md and prints a verdict + Markdown report.

Read-only: never writes to keiba.db or monthly_results_*.json.
"""

import glob
import json
import math
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
MONITORING_MD = ROOT / "docs" / "OPERATIONAL_MONITORING.md"
REPORT_OUT = ROOT / "docs" / "monitoring_report.md"

GRADE_TO_KEY = {
    "新馬": "C2 新馬",
    "未勝利": "F1 未勝利",
    "3勝": "3勝",
    "G1": "G1",
    "G2": "G2",
}

# Buy classes that v6.6 retired. Flag them in the report.
RETIRED_GRADES = {"1勝", "2勝", "G3"}


def parse_baselines():
    """Extract BT baselines from OPERATIONAL_MONITORING.md (no hardcode)."""
    text = MONITORING_MD.read_text(encoding="utf-8")
    overall = {}
    m = re.search(r"7年平均 ROI\s*\|\s*([\d.]+)%", text)
    if m:
        overall["bt_7yr"] = float(m.group(1))
    m = re.search(r"直近2年 ROI\s*\|\s*([\d.]+)%", text)
    if m:
        overall["bt_2yr"] = float(m.group(1))

    classes = {}
    row_re = re.compile(
        r"\|\s*(C2 新馬|F1 未勝利|3勝|G1|G2)\s*\|"
        r"\s*([\d.]+)%\s*\|\s*([\d.]+)%\s*\|\s*([\d.]+)%\s*\|"
    )
    for m in row_re.finditer(text):
        classes[m.group(1)] = {
            "bt": float(m.group(2)),
            "allow": float(m.group(3)),
            "warn": float(m.group(4)),
        }
    return overall, classes


def wilson_ci(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def bootstrap_roi_ci(rows, n_iter=2000, seed=42):
    """rows: list of (cost, return). Deterministic bootstrap 95% CI for ROI."""
    if not rows:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(rows)
    rois = []
    for _ in range(n_iter):
        c = 0
        v = 0
        for _ in range(n):
            cc, rr = rows[rng.randrange(n)]
            c += cc
            v += rr
        if c > 0:
            rois.append(v / c * 100)
    if not rois:
        return (0.0, 0.0)
    rois.sort()
    lo = rois[int(0.025 * len(rois))]
    hi = rois[int(0.975 * len(rois))]
    return (lo, hi)


def aggregate(buys):
    cost = sum(b["cost"] for b in buys)
    ret = sum(b["return"] for b in buys)
    return {
        "n": len(buys),
        "cost": cost,
        "return": ret,
        "profit": ret - cost,
        "roi": (ret / cost * 100) if cost else 0.0,
        "hits": sum(1 for b in buys if b["profit"] > 0),
    }


def verdict(roi, allow=100.0, warn=95.0):
    """Return (tag, label). Bands per OPERATIONAL_MONITORING.md."""
    if roi >= 110:
        return "GREEN+", "想定超え"
    if roi >= allow:
        return "GREEN", "正常"
    if roi >= warn:
        return "YELLOW", "注意"
    return "RED", "警告"


EMOJI = {"GREEN+": "🟢+", "GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}


def load_months():
    files = sorted(glob.glob(str(ROOT / "monthly_results_*.json")))
    return [json.load(open(fp, encoding="utf-8")) for fp in files]


def fmt_line(label, agg, ci=None, base=None):
    s = (
        f"  {label:<18} {agg['n']:>3}R "
        f" cost={agg['cost']:>7,}  ROI={agg['roi']:>6.1f}%  "
        f"P&L={agg['profit']:>+8,}"
    )
    if ci:
        s += f"  CI95=[{ci[0]:>5.1f},{ci[1]:>5.1f}]"
    if base is not None:
        s += f"  BT={base:.1f}%  diff={agg['roi'] - base:+.1f}pt"
    return s


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    overall_base, class_base = parse_baselines()
    months = load_months()
    if not months:
        print("No monthly_results_*.json found.")
        return

    all_buys = [b for m in months for d in m["days"] for b in d.get("buy_results", [])]
    if not all_buys:
        print("No buy_results in any monthly file.")
        return

    cum = aggregate(all_buys)
    cum_rows = [(b["cost"], b["return"]) for b in all_buys]
    cum_ci = bootstrap_roi_ci(cum_rows)
    v_tag, v_label = verdict(cum["roi"])

    bt7 = overall_base.get("bt_7yr", "?")
    bt2 = overall_base.get("bt_2yr", "?")

    print("=" * 78)
    print(f" v6.6 actual monitoring  (cumulative {cum['n']}R, {len(months)} month(s))")
    print("=" * 78)
    print(
        f" Verdict: [{v_tag}] {v_label}   ROI {cum['roi']:.1f}%  "
        f"CI95 [{cum_ci[0]:.1f}, {cum_ci[1]:.1f}]"
    )
    print(f" BT baseline: 7yr {bt7}% / last2yr {bt2}%")
    print(
        f" Cumulative: cost={cum['cost']:,}  ret={cum['return']:,}  "
        f"P&L={cum['profit']:+,}  hits={cum['hits']}/{cum['n']}"
    )
    print()

    print("[1] By month")
    for m in months:
        buys = [b for d in m["days"] for b in d.get("buy_results", [])]
        if not buys:
            continue
        agg = aggregate(buys)
        ci = bootstrap_roi_ci([(b["cost"], b["return"]) for b in buys])
        vt, _ = verdict(agg["roi"])
        print(fmt_line(f"{m['month']}[{vt}]", agg, ci))
    print()

    by_grade = defaultdict(list)
    for b in all_buys:
        by_grade[b.get("grade", "?")].append(b)

    print("[2] By class (cumulative)")
    for grade in sorted(by_grade.keys()):
        buys = by_grade[grade]
        agg = aggregate(buys)
        ci = bootstrap_roi_ci([(b["cost"], b["return"]) for b in buys])
        key = GRADE_TO_KEY.get(grade)
        base = class_base.get(key, {}).get("bt") if key else None
        if key and key in class_base:
            cb = class_base[key]
            vt, _ = verdict(agg["roi"], cb["allow"], cb["warn"])
        elif grade in RETIRED_GRADES:
            vt = "RETIRED"
        else:
            vt = "----"
        print(fmt_line(f"{grade}[{vt}]", agg, ci, base))
    print()

    by_bt = defaultdict(list)
    for b in all_buys:
        by_bt[b.get("buy_type", "?")].append(b)

    print("[3] By buy_type (cumulative)")
    for bt in sorted(by_bt.keys()):
        buys = by_bt[bt]
        agg = aggregate(buys)
        ci = bootstrap_roi_ci([(b["cost"], b["return"]) for b in buys])
        print(fmt_line(bt, agg, ci))
    print()

    # ------ [4] 外れの4タイプ分析 ------
    MISS_LABELS = {
        "luck": "運のぶれ(1-3着)",
        "narrow": "読み甘(4-5着)",
        "misread": "読み違い(6着+)",
        "scenario": "展開依存(会場集中)",
        "unknown": "不明",
    }
    losses = [b for b in all_buys if b.get("profit", 0) <= 0]
    by_miss = defaultdict(list)
    for b in losses:
        mt = b.get("miss_type", "unknown")
        by_miss[mt].append(b)

    if losses:
        print("[4] 外れの4タイプ分析 (凱旋門太郎フレームワーク)")
        total_losses = len(losses)
        for mt in ["luck", "narrow", "misread", "scenario", "unknown"]:
            items = by_miss.get(mt, [])
            if not items:
                continue
            pct = len(items) / total_losses * 100
            avg_finish = sum(b.get("honmei_finish", 0) or 0 for b in items) / len(items)
            label = MISS_LABELS.get(mt, mt)
            print(f"  {label:<22} {len(items):>3}件 ({pct:>5.1f}%)  平均着順{avg_finish:.1f}")
        print()

        # 週次サマリ: 最頻タイプ + 改善方針
        most_common = max(by_miss.keys(), key=lambda k: len(by_miss[k]))
        mc_label = MISS_LABELS.get(most_common, most_common)
        mc_count = len(by_miss[most_common])
        print(f"  [最頻] {mc_label} ({mc_count}/{total_losses}件)")
        advice = {
            "luck": "→ 行動維持が正解。モデルは機能している。サンプル蓄積を待つ",
            "narrow": "→ 見立ては近い。EV閾値の微調整 or ボーナス精度向上を検討",
            "misread": "→ スコアリング要素の振り返り必要。着順分布をクラス別に確認",
            "scenario": "→ 同一会場集中の見送りルール検討。馬場/天候の条件読みを改善",
            "unknown": "→ honmei_finish が記録されていない。データ確認必要",
        }
        print(f"  {advice.get(most_common, '')}")
        print()

    md = []
    md.append("# v6.6 実運用モニタリング レポート\n")
    md.append(f"_累積 {cum['n']}R / 月数 {len(months)} / 自動生成: monitor_actual.py_\n")
    md.append(f"## 判定: {EMOJI.get(v_tag, v_tag)} {v_label}\n")
    md.append(f"- 実ROI: **{cum['roi']:.1f}%**  (CI95 [{cum_ci[0]:.1f}, {cum_ci[1]:.1f}])")
    md.append(f"- BT基準: 7年平均 {bt7}% / 直近2年 {bt2}%")
    md.append(
        f"- 累計損益: **{cum['profit']:+,} 円**  "
        f"(cost {cum['cost']:,} / ret {cum['return']:,})"
    )
    md.append(f"- 的中: {cum['hits']}/{cum['n']}")
    md.append("")
    md.append("## 月別")
    md.append("| 月 | 件数 | ROI | CI95 | 損益 | 判定 |")
    md.append("|---|---|---|---|---|---|")
    for m in months:
        buys = [b for d in m["days"] for b in d.get("buy_results", [])]
        if not buys:
            continue
        agg = aggregate(buys)
        ci = bootstrap_roi_ci([(b["cost"], b["return"]) for b in buys])
        vt, _ = verdict(agg["roi"])
        md.append(
            f"| {m['month']} | {agg['n']}R | {agg['roi']:.1f}% | "
            f"[{ci[0]:.1f}, {ci[1]:.1f}] | {agg['profit']:+,} | "
            f"{EMOJI.get(vt, vt)} |"
        )
    md.append("")
    md.append("## クラス別 (累積)")
    md.append("| クラス | 件数 | 実ROI | BT | 乖離 | CI95 | 判定 |")
    md.append("|---|---|---|---|---|---|---|")
    for grade in sorted(by_grade.keys()):
        buys = by_grade[grade]
        agg = aggregate(buys)
        ci = bootstrap_roi_ci([(b["cost"], b["return"]) for b in buys])
        key = GRADE_TO_KEY.get(grade)
        base = class_base.get(key, {}).get("bt") if key else None
        if key and key in class_base:
            cb = class_base[key]
            vt, _ = verdict(agg["roi"], cb["allow"], cb["warn"])
            mark = EMOJI.get(vt, vt)
        elif grade in RETIRED_GRADES:
            mark = "(retired)"
        else:
            mark = "-"
        diff = f"{agg['roi'] - base:+.1f}pt" if base is not None else "-"
        base_s = f"{base:.1f}%" if base is not None else "-"
        md.append(
            f"| {grade} | {agg['n']}R | {agg['roi']:.1f}% | {base_s} | {diff} | "
            f"[{ci[0]:.1f}, {ci[1]:.1f}] | {mark} |"
        )
    md.append("")
    md.append("## 買い目タイプ別 (累積)")
    md.append("| タイプ | 件数 | ROI | CI95 | 損益 |")
    md.append("|---|---|---|---|---|")
    for bt in sorted(by_bt.keys()):
        buys = by_bt[bt]
        agg = aggregate(buys)
        ci = bootstrap_roi_ci([(b["cost"], b["return"]) for b in buys])
        md.append(
            f"| {bt} | {agg['n']}R | {agg['roi']:.1f}% | "
            f"[{ci[0]:.1f}, {ci[1]:.1f}] | {agg['profit']:+,} |"
        )
    md.append("")
    if losses:
        md.append("## 外れの4タイプ分析")
        md.append("")
        md.append("凱旋門太郎フレームワーク: 外れを分類し改善方向を特定する")
        md.append("")
        md.append("| タイプ | 件数 | 割合 | 平均着順 | 方針 |")
        md.append("|---|---|---|---|---|")
        miss_labels_short = {
            "luck": ("運のぶれ", "行動維持"),
            "narrow": ("読み甘", "EV微調整"),
            "misread": ("読み違い", "要振り返り"),
            "scenario": ("展開依存", "条件読み改善"),
            "unknown": ("不明", "データ確認"),
        }
        for mt in ["luck", "narrow", "misread", "scenario", "unknown"]:
            items = by_miss.get(mt, [])
            if not items:
                continue
            pct = len(items) / total_losses * 100
            avg_f = sum(b.get("honmei_finish", 0) or 0 for b in items) / len(items)
            lbl, act = miss_labels_short.get(mt, (mt, "-"))
            md.append(f"| {lbl} | {len(items)}件 | {pct:.0f}% | {avg_f:.1f}着 | {act} |")
        md.append("")
        mc_lbl = miss_labels_short.get(most_common, (most_common, ""))[0]
        md.append(f"**最頻タイプ: {mc_lbl}** ({mc_count}/{total_losses}件)")
        md.append("")
    md.append("## 注記")
    md.append("- CI95はブートストラップ法 (n=2000, seed=42) で算出")
    md.append("- 1勝/2勝/G3はv6.6で廃止クラス。表示は過去買い目の参考値")
    md.append("- BT基準値は docs/OPERATIONAL_MONITORING.md からパース")
    md.append("- 外れ4タイプ: luck=1-3着, narrow=4-5着, misread=6着+, scenario=同会場3敗+集中")

    REPORT_OUT.write_text("\n".join(md), encoding="utf-8")
    print(f"Markdown report -> {REPORT_OUT}")


if __name__ == "__main__":
    main()
