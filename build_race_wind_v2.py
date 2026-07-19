"""
Phase 0c-v2: race_wind テーブル再構築(venue_geo_v2 対応版)

v1(build_race_wind.py)からの変更点:
  1. venue_geo -> venue_geo_v2 を参照(再測量済みbearing + turn_dir列)。
  2. 新潟のみ venue_variant で 'straight'(芝1000m) / 'loop'(それ以外)を判定し、
     対応するbearingを使う。他9場は venue_variant='default' 固定。

【新潟コース判定ロジックについての注記】
当初計画では新潟の内回り/外回りも区別する想定だったが、OSM実測の結果、
turf外回り/turf内回り/dirt外回り/dirt内回りの4地物すべてのホームストレッチ方位角が
241.3〜242.1°の範囲(スプレッド1°未満)に収束することが判明した。つまり内回り/外回りの
違いはtail_home計算に実質影響しない。そのため実装上は「芝1000m(直線コース)か、
それ以外(周回コース=内回り/外回り/ダート/障害を全て含む)か」の二値分類のみを行う。
JRA公式コース紹介表で芝1000mは直線コース専用と明記されており(内回り/外回りの
発走距離表には1000mが登場しない)、この判定にdistance列以外の追加情報は不要。

出力: race_wind テーブル(v1と同じ列構成 + venue_variant 列を追加)
"""
import sqlite3
import math

DB_PATH = "keiba.db"

POST_TIME_MAP = {
    1: "09:55", 2: "10:25", 3: "10:55", 4: "11:25", 5: "11:55",
    6: "12:35", 7: "13:10", 8: "13:40", 9: "14:10", 10: "14:45",
    11: "15:20", 12: "16:25",
}


def hhmm_to_min(hhmm):
    h, m = hhmm.split(":")
    h, m = int(h), int(m)
    if h == 24:
        return 1440
    return h * 60 + m


def niigata_variant(surface, distance):
    """新潟のみ意味を持つ。芝1000m=直線コース、それ以外=周回コース。"""
    if surface == "芝" and distance == 1000:
        return "straight"
    return "loop"


def build_race_wind(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS race_wind_v2 (
          race_id TEXT PRIMARY KEY,
          date TEXT, venue TEXT, race_num INTEGER, venue_variant TEXT,
          post_time_est TEXT,
          wind_speed_avg REAL, wind_dir_avg_deg REAL,
          tail_home REAL, gust_max REAL, n_obs INTEGER
        )
    """)
    conn.execute("DELETE FROM race_wind_v2")

    # bearings[(venue, variant)] = bearing_deg
    bearings = {}
    for venue, variant, bearing in conn.execute(
        "SELECT venue, venue_variant, straight_bearing_deg FROM venue_geo_v2"
    ):
        bearings[(venue, variant)] = bearing

    # race_laps: race_id, date, venue, race_num, surface, distance
    races = conn.execute("""
        SELECT race_id, date, venue, race_num, surface, distance FROM race_laps
    """).fetchall()

    obs_by_dv = {}
    for row in conn.execute(
        "SELECT date, venue, hhmm, wind_speed, wind_dir_deg, gust_speed FROM weather_obs"
    ):
        date, venue, hhmm, wspd, wdir, gspd = row
        key = (date, venue)
        obs_by_dv.setdefault(key, []).append((hhmm, wspd, wdir, gspd))

    n_ok = n_no_post = n_no_obs = n_no_bearing = 0
    out_rows = []

    for race_id, date, venue, race_num, surface, distance in races:
        post_hhmm = POST_TIME_MAP.get(race_num)
        if post_hhmm is None:
            n_no_post += 1
            continue

        if venue == "新潟":
            variant = niigata_variant(surface, distance)
        else:
            variant = "default"

        bearing = bearings.get((venue, variant))
        if bearing is None:
            n_no_bearing += 1
            continue

        post_min = hhmm_to_min(post_hhmm)
        lo, hi = post_min - 30, post_min + 30

        candidates = obs_by_dv.get((date, venue), [])
        window = []
        for hhmm, wspd, wdir, gspd in candidates:
            try:
                m = hhmm_to_min(hhmm)
            except Exception:
                continue
            if lo <= m <= hi:
                window.append((wspd, wdir, gspd))

        if not window:
            n_no_obs += 1
            continue

        us, vs, speeds, gusts = [], [], [], []
        for wspd, wdir, gspd in window:
            if wspd is not None and wdir is not None:
                rad = math.radians(wdir)
                us.append(wspd * math.sin(rad))
                vs.append(wspd * math.cos(rad))
                speeds.append(wspd)
            if gspd is not None:
                gusts.append(gspd)

        if not us:
            n_no_obs += 1
            continue

        u_avg = sum(us) / len(us)
        v_avg = sum(vs) / len(vs)
        wind_speed_avg = math.hypot(u_avg, v_avg)
        wind_dir_avg_deg = math.degrees(math.atan2(u_avg, v_avg)) % 360

        tail_home = wind_speed_avg * math.cos(
            math.radians((wind_dir_avg_deg + 180) - bearing))

        gust_max = max(gusts) if gusts else None

        out_rows.append((
            race_id, date, venue, race_num, variant, post_hhmm,
            round(wind_speed_avg, 3), round(wind_dir_avg_deg, 1),
            round(tail_home, 3), gust_max, len(window),
        ))
        n_ok += 1

    conn.executemany("""
        INSERT OR REPLACE INTO race_wind_v2
          (race_id, date, venue, race_num, venue_variant, post_time_est,
           wind_speed_avg, wind_dir_avg_deg, tail_home, gust_max, n_obs)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, out_rows)
    conn.commit()

    return {"total": len(races), "ok": n_ok, "no_post": n_no_post,
            "no_bearing": n_no_bearing, "no_obs": n_no_obs}


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    stats = build_race_wind(conn)
    print("race_wind_v2 construction stats:", stats)

    n = conn.execute("SELECT COUNT(*) FROM race_wind_v2").fetchone()[0]
    print(f"race_wind_v2 rows: {n}")

    print("\nNiigata variant breakdown:")
    for r in conn.execute(
        "SELECT venue_variant, COUNT(*) FROM race_wind_v2 WHERE venue='新潟' GROUP BY venue_variant"
    ):
        print(f"  {r[0]}: {r[1]} races")

    print("\nSample (tail_home distribution, overall):")
    for r in conn.execute(
        "SELECT MIN(tail_home), AVG(tail_home), MAX(tail_home), "
        "AVG(wind_speed_avg), AVG(gust_max) FROM race_wind_v2"
    ):
        print(f"  tail_home min/avg/max = {r[0]:.2f} / {r[1]:.2f} / {r[2]:.2f}, "
              f"avg wind_speed={r[3]:.2f}, avg gust_max={r[4]:.2f}")

    conn.close()


if __name__ == "__main__":
    main()
