"""
calibrate_mc_dyn_phase1.py — mc_dyn Phase1の3パラメータ(accel_frac/k0/phi_fade)を
5代表コース合算MAE最小化で簡易グリッド探索する(座標降下法、2ラウンド)。
"""
import sqlite3
import json
from collections import defaultdict

from mc_dyn_engine import simulate_solo

DB_PATH = "keiba.db"
TEST_COURSES = [
    ("阪神", "芝", 1200), ("東京", "芝", 1600), ("東京", "芝", 2000),
    ("中山", "ダ", 1200), ("阪神", "ダ", 1800),
]

CANDIDATES = {
    "accel_frac": [0.85, 0.88, 0.90, 0.93, 0.96],
    "k0": [0.0, 0.4, 0.8, 1.2, 1.6],
    "phi_fade": [0.0, 0.02, 0.04, 0.08],
}


def get_par_time(conn, venue, surface, distance):
    row = conn.execute("""
        WITH ranked AS (
            SELECT race_id, time_sec, ROW_NUMBER() OVER (PARTITION BY race_id ORDER BY finish) rn
            FROM results WHERE venue=? AND surface=? AND distance=? AND finish<90 AND time_sec>0
        )
        SELECT AVG(time_sec), COUNT(*) FROM ranked WHERE rn<=3
    """, (venue, surface, distance)).fetchone()
    return row[0]


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
        return None
    n = max(per_index) + 1
    return [sum(per_index[i]) / len(per_index[i]) if i in per_index else None for i in range(n)]


def mae_for_params(conn, course_data, **params):
    total_err, n_err = 0.0, 0
    for venue, surface, distance, par_time, avg_laps in course_data:
        sim = simulate_solo(distance, par_time, spd=80.0, spr=80.0, sta=75.0, **params)
        seg_lens = sim["seg_lens"]
        sim_laps = sim["laps"]
        n_compare = min(len(avg_laps), len(sim_laps))
        for i in range(n_compare):
            if avg_laps[i] is None:
                continue
            real_200 = avg_laps[i] / seg_lens[i] * 200
            sim_200 = sim_laps[i] / seg_lens[i] * 200
            total_err += abs(real_200 - sim_200)
            n_err += 1
    return total_err / n_err if n_err else float("inf")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")

    course_data = []
    for venue, surface, distance in TEST_COURSES:
        par_time = get_par_time(conn, venue, surface, distance)
        avg_laps = get_mpace_avg_laps(conn, venue, surface, distance)
        if par_time and avg_laps:
            course_data.append((venue, surface, distance, par_time, avg_laps))
    print(f"対象コース: {len(course_data)}/{len(TEST_COURSES)}")

    best = {"accel_frac": 0.90, "k0": 0.8, "phi_fade": 0.04}
    best_mae = mae_for_params(conn, course_data, **best)
    print(f"初期MAE={best_mae:.4f}  {best}")

    for rd in range(2):
        improved = False
        for param in CANDIDATES:
            local_best_val, local_best_mae = best[param], best_mae
            for cand in CANDIDATES[param]:
                trial = dict(best)
                trial[param] = cand
                mae = mae_for_params(conn, course_data, **trial)
                if mae < local_best_mae:
                    local_best_mae, local_best_val = mae, cand
            if local_best_val != best[param]:
                best[param] = local_best_val
                best_mae = local_best_mae
                improved = True
                print(f"  round{rd+1} {param} -> {local_best_val} (MAE={best_mae:.4f})")
        if not improved:
            print(f"  round{rd+1}: 変化なし、収束")
            break

    print(f"\n最終パラメータ: {best}")
    print(f"最終MAE(5コース平均、秒/200m換算): {best_mae:.4f}")

    # コースごとの内訳・par一致確認
    print(f"\n{'='*70}\nコース別詳細(較正後パラメータ)\n{'='*70}")
    n_mae_ok, n_win_ok = 0, 0
    for venue, surface, distance, par_time, avg_laps in course_data:
        sim = simulate_solo(distance, par_time, spd=80.0, spr=80.0, sta=75.0, **best)
        seg_lens = sim["seg_lens"]
        sim_laps = sim["laps"]
        n_compare = min(len(avg_laps), len(sim_laps))
        errs = [abs(avg_laps[i] / seg_lens[i] * 200 - sim_laps[i] / seg_lens[i] * 200)
                for i in range(n_compare) if avg_laps[i] is not None]
        mae = sum(errs) / len(errs) if errs else None
        win_diff = sim["total_time"] - par_time
        ok_mae = mae is not None and mae < 0.3
        ok_win = abs(win_diff) <= 0.3
        n_mae_ok += ok_mae
        n_win_ok += ok_win
        print(f"{venue}{surface}{distance}m: MAE={mae:.3f}{'OK' if ok_mae else 'NG'}  "
              f"勝ち時計差={win_diff:+.3f}秒{'OK' if ok_win else 'NG'}")

    print(f"\nゲート: MAE<0.3 -> {n_mae_ok}/{len(course_data)}  win=par±0.3 -> {n_win_ok}/{len(course_data)}")

    with open("mc_dyn_phase1_params.json", "w", encoding="utf-8") as f:
        json.dump({"params": best, "mae": best_mae, "gate_mae_ok": n_mae_ok,
                    "gate_win_ok": n_win_ok, "n_courses": len(course_data)}, f,
                   ensure_ascii=False, indent=2)
    print("\n保存: mc_dyn_phase1_params.json")
    conn.close()


if __name__ == "__main__":
    main()
