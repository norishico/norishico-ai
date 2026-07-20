"""較正メインドライバ: 座標降下法探索(2021-2022) -> OOS評価(2023/2024/2025) ->
reliability diagram -> Gate2頑健性再チェック。"""
import sqlite3
import time
import json
import math
from collections import defaultdict

import mc123_engine
from calibrate_mc123 import (
    load_year_races, brier_and_logloss, set_coefs, PLACEHOLDER, coordinate_descent,
)
from mc123_batch import load_horse_hist_all, load_same_day_bias_dict
from build_class_par import build_class_par_table, calibrate_k_cls
from build_pace_baseline import build_baseline_table
from build_extra_par import build_rank_par, build_margin_par, build_l3f_par
from mc123_engine import run_mc123, hash64_seed

DB_PATH = "keiba.db"
N_MC_SEARCH = 200
N_MC_FINAL = 500


def main():
    t_total = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    print("=== 一括ロード ===")
    horse_hist = load_horse_hist_all(conn)
    bias_map = load_same_day_bias_dict(conn)
    wind_map = {r[0]: {"tail_home": r[1], "gust_max": r[2]} for r in conn.execute(
        "SELECT race_id, tail_home, gust_max FROM race_wind_v2")}

    # ── 1. 座標降下法探索(train=2021-2022のみ、cutoff=2023-01-01の特徴量) ──
    print("\n=== 探索用データ準備(2021-2022, cutoff=2023-01-01) ===")
    cutoff_train = "2023-01-01"
    class_par_t = build_class_par_table(conn, cutoff_date=cutoff_train, verbose=False)
    k_cls_t = calibrate_k_cls(conn, cutoff_date=cutoff_train, verbose=False)
    pace_baseline_t = build_baseline_table(conn, cutoff_date=cutoff_train, verbose=False)
    rank_par_t = build_rank_par(conn, cutoff_date=cutoff_train, verbose=False)
    margin_par_t = build_margin_par(conn, cutoff_date=cutoff_train, verbose=False)
    l3f_par_t = build_l3f_par(conn, cutoff_date=cutoff_train, verbose=False)
    prepared_train = load_year_races(conn, 2021, 2023, horse_hist, class_par_t, k_cls_t,
                                      bias_map, pace_baseline_t, wind_map,
                                      rank_par=rank_par_t, margin_par=margin_par_t,
                                      l3f_par=l3f_par_t)

    print("\n=== 座標降下法探索(n_mc=200) ===")
    t0 = time.time()
    best_coefs, train_brier, train_ll, n_evals = coordinate_descent(
        prepared_train, n_mc=N_MC_SEARCH, rounds=2)
    print(f"探索所要時間: {time.time()-t0:.1f}s ({n_evals}回評価)")
    print(f"較正後係数: {best_coefs}")

    # ── 2. OOS評価(2023/2024/2025、各年cutoffで特徴量再構築) + reliability diagram用集計を同時実施
    #     (before/after予測を1回ずつ計算し、Brier/loglossとdecileバケットを両方その場で集計。
    #      二重計算を避けて時間を節約する)
    print("\n=== OOS評価(2023/2024/2025) + Reliability diagram 同時集計 ===")
    oos_results = {}
    reliability = {"before": defaultdict(lambda: [0, 0]), "after": defaultdict(lambda: [0, 0])}
    for eval_year in (2023, 2024, 2025):
        cutoff = f"{eval_year}-01-01"
        class_par_e = build_class_par_table(conn, cutoff_date=cutoff, verbose=False)
        k_cls_e = calibrate_k_cls(conn, cutoff_date=cutoff, verbose=False)
        pace_baseline_e = build_baseline_table(conn, cutoff_date=cutoff, verbose=False)
        rank_par_e = build_rank_par(conn, cutoff_date=cutoff, verbose=False)
        margin_par_e = build_margin_par(conn, cutoff_date=cutoff, verbose=False)
        l3f_par_e = build_l3f_par(conn, cutoff_date=cutoff, verbose=False)
        prepared_eval = load_year_races(conn, eval_year, eval_year + 1, horse_hist,
                                         class_par_e, k_cls_e, bias_map, pace_baseline_e,
                                         wind_map, rank_par=rank_par_e, margin_par=margin_par_e,
                                         l3f_par=l3f_par_e, verbose=False)

        year_metrics = {}
        for label, coefs in (("before", PLACEHOLDER), ("after", best_coefs)):
            set_coefs(coefs)
            sq_err_sum, ll_sum, n_obs = 0.0, 0.0, 0
            eps = 1e-6
            for horses, race_info, wind, seed in prepared_eval:
                result = run_mc123(horses, race_info, n_mc=N_MC_FINAL, seed=seed, wind=wind)
                for h, r in zip(horses, result):
                    y = 1.0 if (h["finish"] is not None and float(h["finish"]) == 1.0) else 0.0
                    p = min(max(r["p1"], eps), 1 - eps)
                    sq_err_sum += (p - y) ** 2
                    ll_sum += -(y * math.log(p) + (1 - y) * math.log(1 - p))
                    n_obs += 1
                    decile = min(9, int(r["p1"] * 10))
                    reliability[label][decile][0] += y
                    reliability[label][decile][1] += 1
            year_metrics[label] = (sq_err_sum / n_obs, ll_sum / n_obs, n_obs)

        brier_before, ll_before, n_before = year_metrics["before"]
        brier_after, ll_after, n_after = year_metrics["after"]
        oos_results[eval_year] = {
            "n": n_before,
            "brier_before": round(brier_before, 6), "brier_after": round(brier_after, 6),
            "logloss_before": round(ll_before, 6), "logloss_after": round(ll_after, 6),
        }
        print(f"  {eval_year}: n={n_before}  Brier {brier_before:.6f} -> {brier_after:.6f}  "
              f"logloss {ll_before:.6f} -> {ll_after:.6f}")

    avg_brier_before = sum(r["brier_before"] * r["n"] for r in oos_results.values()) / sum(r["n"] for r in oos_results.values())
    avg_brier_after = sum(r["brier_after"] * r["n"] for r in oos_results.values()) / sum(r["n"] for r in oos_results.values())
    avg_ll_before = sum(r["logloss_before"] * r["n"] for r in oos_results.values()) / sum(r["n"] for r in oos_results.values())
    avg_ll_after = sum(r["logloss_after"] * r["n"] for r in oos_results.values()) / sum(r["n"] for r in oos_results.values())
    print(f"\nOOS平均(n加重): Brier {avg_brier_before:.6f} -> {avg_brier_after:.6f}  "
          f"logloss {avg_ll_before:.6f} -> {avg_ll_after:.6f}")
    improved = avg_brier_after < avg_brier_before

    print("\n=== Reliability diagram (pooled 2023-2025) ===")

    print(f"{'decile(p1)':<14}{'n(before)':>10}{'実勝率(before)':>16}{'n(after)':>10}{'実勝率(after)':>15}")
    for d in range(10):
        b_win, b_n = reliability["before"][d]
        a_win, a_n = reliability["after"][d]
        b_rate = b_win / b_n * 100 if b_n else float("nan")
        a_rate = a_win / a_n * 100 if a_n else float("nan")
        print(f"{d/10:.1f}-{(d+1)/10:.1f}       {b_n:>10}{b_rate:>15.2f}%{a_n:>10}{a_rate:>14.2f}%")

    # ── 4. Gate2頑健性再チェック(較正後係数を固定してF6を再実行) ──
    print("\n=== Gate2 F6フィルタ 頑健性再チェック(較正後係数、事前登録済みグリッドのまま) ===")
    set_coefs(best_coefs)
    import gate2_mc123_wf as g2
    all_bets = []
    for year in g2.FOLD_YEARS:
        bets = g2.run_fold(conn, year, horse_hist, bias_map, fixed_coefs=best_coefs)
        all_bets.extend(bets)
    gate2_results = g2.evaluate_grid(all_bets)
    any_pass = False
    for (x_min, d), r in gate2_results.items():
        print(f"x_min={x_min} d={d}: n={r['n']} WF-ROI={r['roi_winsorized']}% "
              f"年別={r['year_roi']} -> {'合格' if r['passed'] else '不合格'}")
        any_pass = any_pass or r["passed"]
    print(f"Gate2再チェック結論: {'合格セルあり(要注意・多重比較でありp-hackにしない)' if any_pass else '不合格のまま変わらず'}")

    # ── 5. 更新判断 ──
    print("\n" + "=" * 60)
    print(f"総所要時間: {time.time()-t_total:.1f}s")
    print(f"改善判定: Brier {'改善' if improved else '未改善'} "
          f"({avg_brier_before:.6f} -> {avg_brier_after:.6f})")

    conn.close()
    return {
        "best_coefs": best_coefs, "improved": improved,
        "oos_results": oos_results, "avg_brier_before": avg_brier_before,
        "avg_brier_after": avg_brier_after, "avg_ll_before": avg_ll_before,
        "avg_ll_after": avg_ll_after, "gate2_results": {str(k): v for k, v in gate2_results.items()},
        "gate2_any_pass": any_pass,
    }


if __name__ == "__main__":
    result = main()
    with open("calibration_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print("\n-> calibration_result.json 保存完了")
