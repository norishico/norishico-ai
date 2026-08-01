"""
backfill_pace_type_fix.py — fetch_laps.py:calc_derived()の端数区間バグ修正(2026-08-01)を
既存race_lapsテーブル全件に反映する。生のlap_times(既にDB保存済み)から再計算するだけで、
netkeibaへの再取得は不要。

対象列: first_3f, last_3f_race, mid_section, accel_point, pace_type
(n_furlongsとlap_times/cumulative自体は変更しない)
"""
import json
import sqlite3
import time

from fetch_laps import calc_derived

DB_PATH = "keiba.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")

    rows = conn.execute(
        "SELECT race_id, lap_times, distance FROM race_laps WHERE lap_times IS NOT NULL"
    ).fetchall()
    print(f"対象: {len(rows):,}件")

    t0 = time.time()
    updates = []
    changed = 0
    for race_id, lap_times_json, distance in rows:
        try:
            laps = json.loads(lap_times_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not laps or distance is None:
            continue
        derived = calc_derived(laps, distance)
        if not derived:
            continue
        updates.append((
            derived.get("first_3f"), derived.get("last_3f_race"), derived.get("mid_section"),
            derived.get("accel_point"), derived.get("pace_type"), race_id,
        ))

    print(f"再計算完了: {len(updates):,}件  {time.time()-t0:.1f}秒")

    conn.executemany(
        "UPDATE race_laps SET first_3f=?, last_3f_race=?, mid_section=?, "
        "accel_point=?, pace_type=? WHERE race_id=?",
        updates,
    )
    conn.commit()
    print(f"UPDATE完了  総所要時間: {time.time()-t0:.1f}秒")
    conn.close()


if __name__ == "__main__":
    main()
