"""
mc123_bt_results.json の再生成版(現在の本番係数、K_RANK/K_MARGIN/K_L3F込み、K_LAYOFF=0)。
sim_bt_mc123.py(Gate1版)をベースに、rank_par/margin_par/l3f_parの計算を追加しただけの
軽量版。年次cutoff_dateでリーク防止しつつ、mc123_engine.pyの現在の本番係数(較正済み)を
そのまま使う(set_coefsで上書きしない=ファイルの値をそのまま使う)。
"""
import sqlite3
import json
import time
from collections import defaultdict

from build_class_par import build_class_par_table, calibrate_k_cls
from build_pace_baseline import build_baseline_table
from build_extra_par import build_rank_par, build_margin_par, build_l3f_par
from mc123_batch import load_horse_hist_all, load_same_day_bias_dict, precompute_horse_features_fast
from mc123_engine import run_mc123, hash64_seed, N_MC_DEFAULT
from mc_dyn_engine import is_jump_race
import mc123_engine

DB_PATH = "keiba.db"
OUTPUT_PATH = "mc123_bt_results_v2.json"
YEARS = [2021, 2022, 2023, 2024, 2025]


def umaban_to_gate(umaban):
    if not umaban or umaban <= 0:
        return 4
    return min((umaban + 1) // 2, 8)


def main():
    print(f"本番係数使用中: K_ABILITY={mc123_engine.K_ABILITY} GRIT_SCALE={mc123_engine.GRIT_SCALE} "
          f"WIND_A={mc123_engine.WIND_A_PACE_SHIFT} WIND_B={mc123_engine.WIND_B_STYLE} "
          f"WIND_C={mc123_engine.WIND_C_GUST} K_RANK={mc123_engine.K_RANK} "
          f"K_MARGIN={mc123_engine.K_MARGIN} K_L3F={mc123_engine.K_L3F} K_LAYOFF={mc123_engine.K_LAYOFF}")

    t_total = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    horse_hist = load_horse_hist_all(conn)
    bias_map = load_same_day_bias_dict(conn)
    wind_map = {r[0]: {"tail_home": r[1], "gust_max": r[2]} for r in conn.execute(
        "SELECT race_id, tail_home, gust_max FROM race_wind_v2")}

    all_records = []
    for year in YEARS:
        cutoff = f"{year}-01-01"
        t0 = time.time()
        class_par = build_class_par_table(conn, cutoff_date=cutoff, verbose=False)
        k_cls = calibrate_k_cls(conn, cutoff_date=cutoff, verbose=False)
        pace_baseline = build_baseline_table(conn, cutoff_date=cutoff, verbose=False)
        rank_par = build_rank_par(conn, cutoff_date=cutoff, verbose=False)
        margin_par = build_margin_par(conn, cutoff_date=cutoff, verbose=False)
        l3f_par = build_l3f_par(conn, cutoff_date=cutoff, verbose=False)

        races = conn.execute("""
            SELECT DISTINCT r.date, r.venue, r.race_num, r.race_id, r.surface, r.distance, r.track_cond, r.race_name
            FROM results r
            WHERE r.date >= ? AND r.date < ?
              AND r.surface IN ('芝','ダ')
              AND r.race_name NOT LIKE '%新馬%' AND r.race_name NOT LIKE '%未勝利%'
            ORDER BY r.date, r.venue, r.race_num
        """, (f"{year}-01-01", f"{year+1}-01-01")).fetchall()

        # 2026-09-23修正: 障害レース除外が一切なかった(新馬・未勝利のNOT LIKEのみ)。
        # is_jump_race()(距離ベース含む)で明示除外する。
        races = [r for r in races if not is_jump_race(r[7], r[4], r[5])]

        for date, venue, race_num, race_id, srf, dist, tc, _rname in races:
            runners = conn.execute(
                "SELECT horse_name, umaban, jockey, finish, horse_num FROM results WHERE race_id=?",
                (race_id,)
            ).fetchall()
            horses = []
            for hn, uma, jk, fin, hnum in runners:
                hn = (hn or "").strip()
                if not hn:
                    continue
                u = uma if uma is not None else hnum
                horses.append({"horse_name": hn, "umaban": u, "jockey": (jk or "").strip(),
                                "gate": umaban_to_gate(u), "style": None, "finish": fin})
            if len(horses) < 4:
                continue

            race_info = {"venue": venue, "distance": dist or 1600, "track_cond": tc or "良",
                         "num_horses": len(horses), "date": date, "surface": srf, "race_id": race_id}
            precompute_horse_features_fast(horses, race_info, horse_hist, class_par, k_cls,
                                            bias_map, pace_baseline, rank_par=rank_par,
                                            margin_par=margin_par, l3f_par=l3f_par)
            wind = wind_map.get(race_id)
            seed = hash64_seed(race_id)
            try:
                result = run_mc123(horses, race_info, n_mc=N_MC_DEFAULT, seed=seed, wind=wind)
            except Exception:
                continue

            for h, r in zip(horses, result):
                all_records.append({
                    "date": date, "venue": venue, "race_num": race_num, "race_id": race_id,
                    "horse_name": h["horse_name"], "style": h["style"],
                    "p1": r["p1"], "p2": r["p2"], "p3": r["p3"], "ptop3": r["ptop3"],
                    "actual_finish": h["finish"],
                    "win": h["finish"] is not None and float(h["finish"]) == 1.0,
                    "hit_top3": h["finish"] is not None and float(h["finish"]) <= 3.0,
                })
        print(f"  [{year}] {len(races)}R処理  {time.time()-t0:.1f}s")

    conn.close()
    print(f"\n総所要時間: {time.time()-t_total:.1f}s  総レコード: {len(all_records):,}")
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"years": YEARS, "n_mc": N_MC_DEFAULT, "records": all_records}, f,
                   ensure_ascii=False, default=str)
    print(f"-> {OUTPUT_PATH} 保存完了")


if __name__ == "__main__":
    main()
