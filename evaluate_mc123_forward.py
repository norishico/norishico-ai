# -*- coding: utf-8 -*-
"""
evaluate_mc123_forward.py — MC123 Forward検証レポート(2026-11-06新設)

generate_mc123_forecast.py が発走前に保存した予測(mc123_forward/日付.json、先着保存でLOCK済み)
を、keiba.db の確定結果と照合し、確率予測としての品質を測る。ROI(賭けて勝てるか)は見ない。
見るのは「出した確率が現実と合っているか」だけ。

指標(p1=勝率 と ptop3=3着内率 のそれぞれについて):
- Brier score と、一様予測(1/n, 3/n)に対する改善率(BSS)
- Log loss
- 較正の傾きと切片(予測の対数オッズに対するロジスティック回帰。理想は傾き1・切片0)
  ※Brierの改善は較正の良さを意味しない(セカンドオピニオン2026-08-13の指摘)ため必ず併記
- 区間別の信頼性表(予測確率帯ごとの平均予測 vs 実測)
- 1位予想(p1最大 / ptop3最大)の実測的中率 と その平均予測確率

Phase A計測(2026-11-07新設、非表示。ログに detail がある日から集計):
- 組合せ確率(馬連/ワイド/三連複の上位20組)の較正: 予測確率帯ごとの平均予測 vs 実測的中率
- ペース確率 pH/pM/pS(エンジンが試行ごとに引く、手書き式で較正未検証)と、実際のペース
  (race_laps.pace_type、netkeibaラップ由来)の突合: 3クラスBrier・クラス別の予測 vs 実測頻度
- 条件付き確率の価値(オラクル): 実ペースを条件にした確率が周辺確率よりBrierを下げるか。
  実ペースは発走前には不明なので、これは「ペース情報の価値の上限」であり実際の改善量ではない

除外(件数は必ずレポートに出す):
- 結果が未確定 / 取消・除外馬が混ざる(確率の質量が欠けるため) / 馬場状態が不明
- generated_at の日付がレース日より後(as_of監査: 結果を見た後の予測の混入防止)

使い方: py -3 evaluate_mc123_forward.py [--forward-dir DIR] [--out PATH] [--db keiba.db]
"""
import sys
import json
import math
import sqlite3
from pathlib import Path

import numpy as np

# results.track_cond(DB主流は1文字、一部2文字)→ MC123の馬場パターン名
TRACK_TO_PATTERN = {"良": "良・稍重", "稍": "良・稍重", "稍重": "良・稍重",
                    "重": "重", "不": "不良", "不良": "不良"}
MIN_HORSES_FOR_CONCLUSION = 500  # これ未満は「暫定」。較正の傾きの標準誤差が大きすぎる
BINS_P1 = [0, 0.03, 0.06, 0.10, 0.15, 0.20, 0.30, 0.40, 1.0001]
BINS_TOP3 = [0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 1.0001]
BINS_COMBO = [0, 0.01, 0.02, 0.04, 0.06, 0.10, 0.15, 0.20, 0.30, 0.50, 1.0001]


def _opt(args, name, default):
    if name in args:
        i = args.index(name)
        v = args[i + 1]
        del args[i:i + 2]
        return v
    return default


def logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def calibration_slope_intercept(p, y, iters=50):
    """y ~ sigmoid(a + b*logit(p)) をIRLSで当てはめ、(傾きb, 切片a, 傾きの標準誤差)を返す。"""
    x = logit(p)
    X = np.column_stack([np.ones_like(x), x])
    beta = np.array([0.0, 1.0])
    for _ in range(iters):
        eta = X @ beta
        mu = 1 / (1 + np.exp(-eta))
        w = np.clip(mu * (1 - mu), 1e-9, None)
        H = X.T @ (X * w[:, None])
        g = X.T @ (y - mu)
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            return None
        beta = beta + step
        if np.max(np.abs(step)) < 1e-8:
            break
    try:
        se = np.sqrt(np.diag(np.linalg.inv(H)))
    except np.linalg.LinAlgError:
        return None
    return {"slope": float(beta[1]), "intercept": float(beta[0]), "slope_se": float(se[1])}


def reliability_table(p, y, edges):
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi)
        if m.sum() == 0:
            continue
        out.append({"range": f"{lo:.2f}-{min(hi, 1.0):.2f}", "n": int(m.sum()),
                    "mean_pred": round(float(p[m].mean()), 4), "observed": round(float(y[m].mean()), 4)})
    return out


def metrics(p, y, base):
    brier = float(np.mean((p - y) ** 2))
    brier0 = float(np.mean((base - y) ** 2))
    pc = np.clip(p, 1e-6, 1 - 1e-6)
    ll = float(-np.mean(y * np.log(pc) + (1 - y) * np.log(1 - pc)))
    return {"n": int(len(p)), "brier": round(brier, 5), "brier_uniform": round(brier0, 5),
            "bss_vs_uniform": round(1 - brier / brier0, 4), "logloss": round(ll, 5),
            "mean_pred": round(float(p.mean()), 4), "observed": round(float(y.mean()), 4),
            "calibration": calibration_slope_intercept(p, y)}


def load_results(conn, race_id):
    rows = conn.execute("SELECT TRIM(horse_name), finish, track_cond FROM results WHERE race_id = ?",
                        (race_id,)).fetchall()
    finish = {r[0]: r[1] for r in rows}
    conds = [r[2] for r in rows if r[2]]
    return finish, (conds[0] if conds else None)


def realized_pace(conn, race_id):
    row = conn.execute("SELECT pace_type FROM race_laps WHERE race_id = ?", (race_id,)).fetchone()
    return row[0] if row and row[0] in ("H", "M", "S") else None


def collect_detail(det, horses, fins, p1, p3, race_id, conn, acc):
    """Phase A計測用の集計(acc: 蓄積辞書)。detailは馬番基準。"""
    acc["n_detail_races"] += 1
    um = [h["umaban"] for h in horses]
    by_um = dict(zip(um, fins))
    top3 = {u for u, f in by_um.items() if f <= 3}
    first_second = {u for u, f in by_um.items() if f in (1, 2)}
    if len(top3) == 3 and len(first_second) == 2:  # 同着があると組の定義が曖昧になるため除外
        truth = {"umaren": lambda c: set(c) == first_second, "wide": lambda c: set(c) <= top3,
                 "sanrenpuku": lambda c: set(c) == top3}
        for key in ("umaren", "wide", "sanrenpuku"):
            for row in det[key]:
                acc["combo"][key][0].append(row[-1])
                acc["combo"][key][1].append(float(truth[key](row[:-1])))
    else:
        acc["n_tie_skip"] += 1
    pace = realized_pace(conn, race_id)
    if pace is None:
        return
    acc["n_pace_label"] += 1
    pp = det["pace_probs"]
    acc["pace_rows"].append((pp["H"], pp["M"], pp["S"], "HMS".index(pace)))
    c = det["cond"].get(pace)
    if c:
        for u, f, m1, m3 in zip(um, fins, p1, p3):
            x = c[str(u)]
            acc["cond"]["p1"][0].append(float(m1)); acc["cond"]["p1"][1].append(x[0])
            acc["cond"]["p1"][2].append(float(f == 1))
            acc["cond"]["ptop3"][0].append(float(m3)); acc["cond"]["ptop3"][1].append(sum(x))
            acc["cond"]["ptop3"][2].append(float(f <= 3))


def summarize_detail(acc):
    out = {"races_with_detail": acc["n_detail_races"], "races_skipped_ties": acc["n_tie_skip"],
           "races_with_pace_label": acc["n_pace_label"]}
    combos = {}
    for key, (pr, y) in acc["combo"].items():
        if not pr:
            continue
        pr, y = np.array(pr), np.array(y)
        combos[key] = {"n_combos": int(len(pr)), "mean_pred": round(float(pr.mean()), 4),
                       "observed": round(float(y.mean()), 4),
                       "brier": round(float(np.mean((pr - y) ** 2)), 5),
                       "reliability": reliability_table(pr, y, BINS_COMBO)}
    out["combos_top20_by_pred"] = combos
    out["combos_note"] = "各レース各券種の予測上位20組のみ(予測値で選抜、結果は見ていないので選抜バイアスなし)"
    if acc["pace_rows"]:
        arr = np.array([r[:3] for r in acc["pace_rows"]])
        real = np.array([r[3] for r in acc["pace_rows"]])
        Y = np.eye(3)[real]
        prior = np.array([0.25, 0.40, 0.35])
        out["pace_probs"] = {
            "n_races": int(len(arr)),
            "mean_pred": {k: round(float(arr[:, i].mean()), 4) for i, k in enumerate("HMS")},
            "observed_freq": {k: round(float(Y[:, i].mean()), 4) for i, k in enumerate("HMS")},
            "brier_3class": round(float(np.mean(np.sum((arr - Y) ** 2, axis=1))), 5),
            "brier_3class_fixed_prior_25_40_35": round(float(np.mean(np.sum((prior - Y) ** 2, axis=1))), 5),
            "note": "pH/pM/pSは手書き式。観測頻度と平均予測が離れていれば『ハイ40%』等の表示は未較正"}
    cv = {}
    for k, (m, c, y) in acc["cond"].items():
        if not y:
            continue
        m, c, y = np.array(m), np.array(c), np.array(y)
        cv[k] = {"n_horses": int(len(y)), "brier_marginal": round(float(np.mean((m - y) ** 2)), 5),
                 "brier_conditioned_on_realized_pace": round(float(np.mean((c - y) ** 2)), 5)}
    if cv:
        out["conditioning_value_oracle"] = cv
        out["conditioning_note"] = "実ペースは発走前に不明。ペース情報の価値の上限(オラクル)であり実際の改善量ではない"
    return out


def main():
    args = sys.argv[1:]
    forward_dir = Path(_opt(args, "--forward-dir", "mc123_forward"))
    out_path = Path(_opt(args, "--out", "mc123_forward_report.json"))
    db = _opt(args, "--db", "keiba.db")

    files = sorted(forward_dir.glob("*.json"))
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    excl = {"unresolved": 0, "scratched_or_missing": 0, "track_unknown": 0, "late_log": 0}
    used_races = 0
    P1, Y1, B1, P3, Y3, B3 = [], [], [], [], [], []
    top1_win, top1_place, hi_conf = [], [], []
    logged = 0
    acc = {"n_detail_races": 0, "n_tie_skip": 0, "n_pace_label": 0, "pace_rows": [],
           "combo": {k: ([], []) for k in ("umaren", "wide", "sanrenpuku")},
           "cond": {"p1": ([], [], []), "ptop3": ([], [], [])}}
    pattern_count = {}
    track_unknown_by_surface = {}
    engine_tags = {}
    for f in files:
        doc = json.loads(f.read_text(encoding="utf-8"))
        for race_id, r in doc["races"].items():
            logged += 1
            engine_tags[r.get("engine_tag", "?")] = engine_tags.get(r.get("engine_tag", "?"), 0) + 1
            if r["generated_at"][:10] > r["race_date"]:
                excl["late_log"] += 1
                continue
            finish, cond = load_results(conn, race_id)
            top = {fin for fin in finish.values() if fin in (1, 2, 3)}
            if top != {1, 2, 3}:
                excl["unresolved"] += 1
                continue
            pat = TRACK_TO_PATTERN.get(cond)
            if pat is None or pat not in r["patterns"]:
                excl["track_unknown"] += 1
                # 2026年3月以降、results.track_cond がダートで系統的にNULLになる既知の問題があり、
                # 除外が芝/ダに偏る。偏りを見えるようにレポートへ残す
                track_unknown_by_surface[r["surface"]] = track_unknown_by_surface.get(r["surface"], 0) + 1
                continue
            horses, rows = r["horses"], r["patterns"][pat]
            fins = [finish.get(h["name"].strip()) for h in horses]
            # 取消・除外(finish NULLまたは90以上)や馬名不一致が混ざるレースは、確率質量が欠けるため除外
            if any(x is None or x >= 90 for x in fins) or len(finish) != len(horses):
                excl["scratched_or_missing"] += 1
                continue
            used_races += 1
            pattern_count[pat] = pattern_count.get(pat, 0) + 1
            n = len(horses)
            p1 = np.array([x["p1"] for x in rows])
            p3 = np.array([x["ptop3"] for x in rows])
            fin = np.array(fins)
            P1.append(p1); Y1.append((fin == 1).astype(float)); B1.append(np.full(n, 1.0 / n))
            P3.append(p3); Y3.append((fin <= 3).astype(float)); B3.append(np.full(n, min(3, n) / n))
            i1 = int(np.argmax(p1))
            top1_win.append((float(p1[i1]), float(fin[i1] == 1)))
            i3 = int(np.argmax(p3))
            top1_place.append((float(p3[i3]), float(fin[i3] <= 3)))
            if p3[i3] >= 0.6:
                hi_conf.append(float(fin[i3] <= 3))
            det = (r.get("detail") or {}).get(pat)
            if det:
                collect_detail(det, horses, fins, p1, p3, race_id, conn, acc)
    conn.close()

    report = {"races_logged": logged, "races_evaluated": used_races, "excluded": excl,
              "track_unknown_by_surface": track_unknown_by_surface,
              "engine_tags": engine_tags, "track_pattern_used": pattern_count}
    if used_races:
        P1, Y1, B1 = map(np.concatenate, (P1, Y1, B1))
        P3, Y3, B3 = map(np.concatenate, (P3, Y3, B3))
        report["n_horses"] = int(len(P1))
        report["status"] = ("暫定(馬数<%d、較正の傾きの誤差が大きい)" % MIN_HORSES_FOR_CONCLUSION
                            if len(P1) < MIN_HORSES_FOR_CONCLUSION else "判定可能")
        report["win_p1"] = metrics(P1, Y1, B1)
        report["win_p1"]["reliability"] = reliability_table(P1, Y1, BINS_P1)
        report["top3_ptop3"] = metrics(P3, Y3, B3)
        report["top3_ptop3"]["reliability"] = reliability_table(P3, Y3, BINS_TOP3)
        tw = np.array(top1_win)
        tp = np.array(top1_place)
        report["top1_by_p1"] = {"n": len(tw), "win_rate": round(float(tw[:, 1].mean()), 4),
                                "mean_pred_p1": round(float(tw[:, 0].mean()), 4)}
        report["top1_by_ptop3"] = {"n": len(tp), "place_rate": round(float(tp[:, 1].mean()), 4),
                                   "mean_pred_ptop3": round(float(tp[:, 0].mean()), 4)}
        # 「ptop3>=60%の1位予想は的中率が高い」仮説(n=652の過去データ由来)の未使用将来データでの検証用
        report["hypothesis_ptop3_ge_0.6"] = {
            "n": len(hi_conf), "place_rate": round(float(np.mean(hi_conf)), 4) if hi_conf else None,
            "note": "仮説(過去652Rで62%)。独立した将来データでの検証。nが小さいうちは結論を出さない"}
        if acc["n_detail_races"]:
            report["phase_a_detail"] = summarize_detail(acc)
    else:
        report["status"] = "評価可能なレースなし(結果未反映、または記録開始直後)"

    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"→ {out_path}")


if __name__ == "__main__":
    main()
