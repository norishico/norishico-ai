"""
test_mc_dyn_phase1.py — mc_dyn Phase 1のゲート検証。
5代表コース(芝1200/1600/2000、ダ1200/1800)で、Mペースレースの実測平均ラップ曲線と
SPD=80のシミュレーション出力を比較し、MAE<0.3秒/F・勝ち時計中央値=par±0.3秒を確認する。
"""
import sqlite3
import json
from collections import defaultdict

from mc_dyn_engine import simulate_solo, segment_lengths

DB_PATH = "keiba.db"

TEST_COURSES = [
    ("阪神", "芝", 1200),
    ("東京", "芝", 1600),
    ("東京", "芝", 2000),
    ("中山", "ダ", 1200),
    ("阪神", "ダ", 1800),
]


def get_par_time(conn, venue, surface, distance):
    row = conn.execute("""
        WITH ranked AS (
            SELECT race_id, time_sec, ROW_NUMBER() OVER (PARTITION BY race_id ORDER BY finish) rn
            FROM results WHERE venue=? AND surface=? AND distance=? AND finish<90 AND time_sec>0
        )
        SELECT AVG(time_sec), COUNT(*) FROM ranked WHERE rn<=3
    """, (venue, surface, distance)).fetchone()
    return row[0], row[1]


def get_mpace_avg_laps(conn, venue, surface, distance):
    rows = conn.execute("""
        SELECT lap_times FROM race_laps
        WHERE venue=? AND surface=? AND distance=? AND pace_type='M' AND lap_times IS NOT NULL
    """, (venue, surface, distance)).fetchall()
    per_index = defaultdict(list)
    for (lap_json,) in rows:
        try:
            laps = json.loads(lap_json)
        except Exception:
            continue
        for i, lap in enumerate(laps):
            per_index[i].append(lap)
    if not per_index:
        return None, 0
    n = max(per_index) + 1
    avg = [sum(per_index[i]) / len(per_index[i]) if i in per_index else None for i in range(n)]
    return avg, len(rows)


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")

    results = []
    for venue, surface, distance in TEST_COURSES:
        par_time, n_par = get_par_time(conn, venue, surface, distance)
        if par_time is None:
            print(f"{venue}{surface}{distance}m: parタイム取得不可、スキップ")
            continue

        avg_laps, n_mpace = get_mpace_avg_laps(conn, venue, surface, distance)
        if avg_laps is None:
            print(f"{venue}{surface}{distance}m: Mペースレースなし、スキップ")
            continue

        sim = simulate_solo(distance, par_time, spd=80.0, spr=80.0, sta=75.0)
        sim_laps = sim["laps"]
        seg_lens = sim["seg_lens"]

        n_compare = min(len(avg_laps), len(sim_laps))
        errors_per_furlong = []  # 秒/200m換算で誤差を揃える
        for i in range(n_compare):
            if avg_laps[i] is None:
                continue
            real_pace_200 = avg_laps[i] / seg_lens[i] * 200
            sim_pace_200 = sim_laps[i] / seg_lens[i] * 200
            errors_per_furlong.append(abs(real_pace_200 - sim_pace_200))

        mae = sum(errors_per_furlong) / len(errors_per_furlong) if errors_per_furlong else None
        win_diff = sim["total_time"] - par_time

        print(f"\n=== {venue}{surface}{distance}m (par_time n={n_par}, Mペースn={n_mpace}) ===")
        print(f"  par_time={par_time:.2f}秒  シミュ勝ち時計={sim['total_time']:.2f}秒  差={win_diff:+.2f}秒")
        print(f"  v_base(SPD=80)={sim['v_base']:.3f} m/s")
        print(f"  ラップMAE(秒/200m換算)={mae:.3f}" if mae is not None else "  MAE計算不可")
        print(f"  実測M平均ラップ: {[round(x,2) if x else None for x in avg_laps[:n_compare]]}")
        print(f"  シミュラップ:     {[round(x,2) for x in sim_laps[:n_compare]]}")

        results.append({
            "venue": venue, "surface": surface, "distance": distance,
            "par_time": par_time, "sim_time": sim["total_time"], "win_diff": win_diff,
            "mae_per_200m": mae, "n_par": n_par, "n_mpace": n_mpace,
        })

    print(f"\n{'='*70}\nゲート判定\n{'='*70}")
    mae_ok = sum(1 for r in results if r["mae_per_200m"] is not None and r["mae_per_200m"] < 0.3)
    win_ok = sum(1 for r in results if abs(r["win_diff"]) <= 0.3)
    print(f"MAE<0.3秒/F: {mae_ok}/{len(results)}コース")
    print(f"勝ち時計=par±0.3秒: {win_ok}/{len(results)}コース")

    with open("mc_dyn_phase1_verdict.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print("\n保存: mc_dyn_phase1_verdict.json")
    conn.close()


if __name__ == "__main__":
    main()
