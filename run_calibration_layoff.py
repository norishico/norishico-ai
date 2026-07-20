"""較正ラウンド3: K_LAYOFF(鮮度/休み明けペナルティ)の較正。

手順: Step1(K_LAYOFFのみ座標降下、他8係数はPROD_BASELINEで固定)
      -> Step2(収束後、念のため全9係数で1周確認)
      -> OOS評価(2023/2024/2025、"before"は必ずPROD_BASELINE=K_LAYOFF=0の現行本番モデル)
      -> Gate2頑健性再チェック。

【baseline取り違えミス再発防止】"before"には必ずPROD_BASELINE(K_LAYOFF=0)を使う。
探索の出発点(WARM_START、K_LAYOFF=0.4)を採否判定のbeforeとして絶対に使わないこと。
"""
import sqlite3
import time
import json
import math
from collections import defaultdict

import mc123_engine
from calibrate_mc123 import (
    load_year_races, brier_and_logloss, set_coefs, PROD_BASELINE, WARM_START,
    coordinate_descent,
)
from mc123_batch import load_horse_hist_all, load_same_day_bias_dict
from build_class_par import build_class_par_table, calibrate_k_cls
from build_pace_baseline import build_baseline_table
from build_extra_par import build_rank_par, build_margin_par, build_l3f_par
from mc123_engine import run_mc123, hash64_seed

DB_PATH = "keiba.db"
N_MC_SEARCH = 200
N_MC_FINAL = 500
REQUIRED_REL_IMPROVEMENT_PCT = 0.44


def build_fold_tables(conn, cutoff):
    class_par = build_class_par_table(conn, cutoff_date=cutoff, verbose=False)
    k_cls = calibrate_k_cls(conn, cutoff_date=cutoff, verbose=False)
    pace_baseline = build_baseline_table(conn, cutoff_date=cutoff, verbose=False)
    rank_par = build_rank_par(conn, cutoff_date=cutoff, verbose=False)
    margin_par = build_margin_par(conn, cutoff_date=cutoff, verbose=False)
    l3f_par = build_l3f_par(conn, cutoff_date=cutoff, verbose=False)
    return class_par, k_cls, pace_baseline, rank_par, margin_par, l3f_par


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

    # ── 訓練データ準備(2021-2022, cutoff=2023-01-01) ──
    print("\n=== 訓練データ準備(2021-2022, cutoff=2023-01-01) ===")
    cutoff_train = "2023-01-01"
    tables_t = build_fold_tables(conn, cutoff_train)
    prepared_train = load_year_races(conn, 2021, 2023, horse_hist, tables_t[0], tables_t[1],
                                      bias_map, tables_t[2], wind_map, rank_par=tables_t[3],
                                      margin_par=tables_t[4], l3f_par=tables_t[5])

    # ── Step1: K_LAYOFFのみ座標降下(他8係数はPROD_BASELINEで固定、WARM_STARTから開始) ──
    print("\n=== Step1: K_LAYOFFのみ探索(n_mc=200) ===")
    t0 = time.time()
    step1_coefs, step1_brier, step1_ll, n_evals1 = coordinate_descent(
        prepared_train, n_mc=N_MC_SEARCH, rounds=1, start=WARM_START, params=["K_LAYOFF"])
    print(f"Step1所要時間: {time.time()-t0:.1f}s ({n_evals1}回評価)  K_LAYOFF={step1_coefs['K_LAYOFF']}")

    # ── Step2: 収束後、念のため全9係数で1周確認 ──
    print("\n=== Step2: 全9係数で1周確認(n_mc=200) ===")
    t0 = time.time()
    best_coefs, train_brier, train_ll, n_evals2 = coordinate_descent(
        prepared_train, n_mc=N_MC_SEARCH, rounds=1, start=step1_coefs, params=None)
    print(f"Step2所要時間: {time.time()-t0:.1f}s ({n_evals2}回評価)")
    print(f"最終較正係数: {best_coefs}")

    # ── OOS評価(2023/2024/2025) — beforeは必ずPROD_BASELINE(K_LAYOFF=0)で再計算 ──
    print("\n=== OOS評価(2023/2024/2025)。before=PROD_BASELINE(K_LAYOFF=0の現行本番モデル) ===")
    oos_results = {}
    for eval_year in (2023, 2024, 2025):
        cutoff = f"{eval_year}-01-01"
        t_e = build_fold_tables(conn, cutoff)
        prepared_eval = load_year_races(conn, eval_year, eval_year + 1, horse_hist,
                                         t_e[0], t_e[1], bias_map, t_e[2], wind_map,
                                         rank_par=t_e[3], margin_par=t_e[4], l3f_par=t_e[5],
                                         verbose=False)

        set_coefs(PROD_BASELINE)
        brier_before, ll_before, n_before = brier_and_logloss(prepared_eval, n_mc=N_MC_FINAL)

        set_coefs(best_coefs)
        brier_after, ll_after, n_after = brier_and_logloss(prepared_eval, n_mc=N_MC_FINAL)

        oos_results[eval_year] = {
            "n": n_before,
            "brier_before": round(brier_before, 6), "brier_after": round(brier_after, 6),
            "logloss_before": round(ll_before, 6), "logloss_after": round(ll_after, 6),
        }
        rel = (brier_before - brier_after) / brier_before * 100
        print(f"  {eval_year}: n={n_before}  Brier {brier_before:.6f} -> {brier_after:.6f} "
              f"(相対改善{rel:+.3f}%)  logloss {ll_before:.6f} -> {ll_after:.6f}")

    n_total = sum(r["n"] for r in oos_results.values())
    avg_brier_before = sum(r["brier_before"] * r["n"] for r in oos_results.values()) / n_total
    avg_brier_after = sum(r["brier_after"] * r["n"] for r in oos_results.values()) / n_total
    avg_ll_before = sum(r["logloss_before"] * r["n"] for r in oos_results.values()) / n_total
    avg_ll_after = sum(r["logloss_after"] * r["n"] for r in oos_results.values()) / n_total
    rel_pooled = (avg_brier_before - avg_brier_after) / avg_brier_before * 100
    print(f"\nOOS平均(n加重): Brier {avg_brier_before:.6f} -> {avg_brier_after:.6f} "
          f"(pooled相対改善{rel_pooled:+.4f}%)")
    print(f"logloss {avg_ll_before:.6f} -> {avg_ll_after:.6f}")

    all_years_improved = all(
        oos_results[y]["brier_after"] < oos_results[y]["brier_before"] for y in (2023, 2024, 2025)
    )
    meets_threshold = rel_pooled >= REQUIRED_REL_IMPROVEMENT_PCT
    accepted = all_years_improved and meets_threshold
    print(f"\n判定: 3年すべて個別改善={all_years_improved}, "
          f"pooled相対改善{rel_pooled:.4f}%>=閾値{REQUIRED_REL_IMPROVEMENT_PCT}%={meets_threshold}")
    print(f"=> 採否: {'合格(本番反映)' if accepted else '不合格(見送り)'}")

    # ── Gate2頑健性再チェック(較正後係数を固定してF6を再実行) ──
    print("\n=== Gate2 F6フィルタ 頑健性再チェック ===")
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
    print(f"Gate2再チェック結論: {'合格セルあり' if any_pass else '不合格のまま変わらず'}")

    print("\n" + "=" * 60)
    print(f"総所要時間: {time.time()-t_total:.1f}s")

    conn.close()
    return {
        "step1_k_layoff": step1_coefs["K_LAYOFF"], "best_coefs": best_coefs,
        "accepted": accepted, "all_years_improved": all_years_improved,
        "rel_pooled_pct": round(rel_pooled, 4), "oos_results": oos_results,
        "avg_brier_before": avg_brier_before, "avg_brier_after": avg_brier_after,
        "avg_ll_before": avg_ll_before, "avg_ll_after": avg_ll_after,
        "gate2_results": {str(k): v for k, v in gate2_results.items()}, "gate2_any_pass": any_pass,
    }


if __name__ == "__main__":
    result = main()
    with open("calibration_result_layoff.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print("\n-> calibration_result_layoff.json 保存完了")
