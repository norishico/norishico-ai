"""analyze_f1_multiyear.py - F1未勝利 全年オッズ帯層別

目的: 2024単年で発見した「15-18倍だけ黒字、18-22倍全敗」が
      他年(2022/2023/2025)でも再現するかを検証し、
      構造的パターンか単年ノイズかを判別する。
"""
import io
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parent
YEARS = [2022, 2023, 2024, 2025]
BANDS = [
    ("15-18", 15, 18),
    ("18-22", 18, 22),
    ("22-26", 22, 26),
    ("26-30", 26, 30),
    ("30-33", 30, 33),
]


def load_f1(y):
    p = ROOT / f"btv6_{y}.json"
    if not p.exists():
        return []
    return [b for b in json.loads(p.read_text(encoding="utf-8"))["bet_records"]
            if b.get("rule") == "F1_未勝利主流accel"]


def band_of(o):
    for name, lo, hi in BANDS:
        if lo <= o < hi:
            return name
    return "?"


def main():
    all_bets = {}
    for y in YEARS:
        all_bets[y] = load_f1(y)
        if not all_bets[y]:
            print(f"[warn] btv6_{y}.json missing")

    # 年×オッズ帯マトリクス
    print("=" * 78)
    print("F1未勝利 年次×オッズ帯 ROI マトリクス")
    print("=" * 78)
    hdr = f"{'年':<6}" + "".join(f"{b[0]:>14}" for b in BANDS) + f"{'合計':>14}"
    print(hdr)
    print("-" * len(hdr))

    totals_by_band = defaultdict(lambda: {"n": 0, "cost": 0, "ret": 0, "win": 0})
    for y in YEARS:
        bets = all_bets[y]
        if not bets:
            continue
        by_band = defaultdict(lambda: {"n": 0, "cost": 0, "ret": 0, "win": 0})
        for b in bets:
            o = b.get("honmei_odds") or 0
            k = band_of(o)
            d = by_band[k]
            d["n"] += 1
            d["cost"] += b.get("cost", 1000)
            d["ret"] += b.get("ret", 0)
            if b.get("honmei_finish") == 1:
                d["win"] += 1
            # accumulate total
            dt = totals_by_band[k]
            dt["n"] += 1; dt["cost"] += b.get("cost", 1000)
            dt["ret"] += b.get("ret", 0)
            if b.get("honmei_finish") == 1:
                dt["win"] += 1

        line = f"{y:<6}"
        for bname, _, _ in BANDS:
            s = by_band[bname]
            if s["n"] == 0:
                cell = "      -/-"
            else:
                roi = s["ret"] / s["cost"] * 100 if s["cost"] else 0
                cell = f"{s['n']:>3}R/{roi:>5.0f}%"
            line += f"{cell:>14}"
        # 年合計
        yc = sum(b.get("cost", 1000) for b in bets)
        yr = sum(b.get("ret", 0) for b in bets)
        yw = sum(1 for b in bets if b.get("honmei_finish") == 1)
        roi_y = yr / yc * 100 if yc else 0
        line += f"{len(bets):>4}R/{roi_y:>5.0f}%"
        print(line)

    # 全年合計行
    print("-" * len(hdr))
    line = f"{'ALL':<6}"
    total_n = total_c = total_r = total_w = 0
    for bname, _, _ in BANDS:
        s = totals_by_band[bname]
        total_n += s["n"]; total_c += s["cost"]; total_r += s["ret"]; total_w += s["win"]
        if s["n"] == 0:
            cell = "      -/-"
        else:
            roi = s["ret"] / s["cost"] * 100 if s["cost"] else 0
            cell = f"{s['n']:>3}R/{roi:>5.0f}%"
        line += f"{cell:>14}"
    roi_all = total_r / total_c * 100 if total_c else 0
    line += f"{total_n:>4}R/{roi_all:>5.0f}%"
    print(line)

    # 詳細: 帯別の全年サマリ
    print("\n" + "=" * 78)
    print("F1未勝利 全年合計 オッズ帯別")
    print("=" * 78)
    print(f"{'band':<8}{'N':>5}{'cost':>10}{'ret':>10}{'profit':>12}{'ROI':>8}{'win%':>7}")
    for bname, _, _ in BANDS:
        s = totals_by_band[bname]
        if s["n"] == 0:
            continue
        roi = s["ret"] / s["cost"] * 100 if s["cost"] else 0
        winp = s["win"] / s["n"] * 100 if s["n"] else 0
        prof = s["ret"] - s["cost"]
        print(f"{bname:<8}{s['n']:>5}{s['cost']:>10,}{s['ret']:>10,}{prof:>+12,}{roi:>7.1f}%{winp:>6.1f}%")

    # 15-18倍だけに絞ったら各年どうなるか
    print("\n" + "=" * 78)
    print("仮: F1を 15-18倍 のみに絞った場合 (全年シミュレーション)")
    print("=" * 78)
    print(f"{'年':<6}{'N':>5}{'profit':>12}{'ROI':>8}{'win%':>7}")
    total_c2 = total_r2 = 0
    for y in YEARS:
        bets = [b for b in all_bets[y] if 15 <= (b.get("honmei_odds") or 0) < 18]
        if not bets:
            continue
        c = sum(b.get("cost", 1000) for b in bets)
        r = sum(b.get("ret", 0) for b in bets)
        w = sum(1 for b in bets if b.get("honmei_finish") == 1)
        roi = r / c * 100 if c else 0
        print(f"{y:<6}{len(bets):>5}{r-c:>+12,}{roi:>7.1f}%{w/len(bets)*100:>6.1f}%")
        total_c2 += c; total_r2 += r
    print("-" * 40)
    print(f"{'合計':<6}{'-':>5}{total_r2-total_c2:>+12,}{(total_r2/total_c2*100 if total_c2 else 0):>7.1f}%")

    # 現行 15-33倍 との比較
    print("\n現行 (15-33倍) vs 15-18倍絞り:")
    all_c = sum(b.get("cost", 1000) for y in YEARS for b in all_bets[y])
    all_r = sum(b.get("ret", 0) for y in YEARS for b in all_bets[y])
    print(f"  現行: {all_r-all_c:+,} / ROI {all_r/all_c*100:.1f}%")
    print(f"  絞り: {total_r2-total_c2:+,} / ROI {(total_r2/total_c2*100 if total_c2 else 0):.1f}%")
    print(f"  差分: {(total_r2-total_c2)-(all_r-all_c):+,}")


if __name__ == "__main__":
    main()
