"""
Phase 0c: race_wind テーブル構築
「風データ×展開シミュレーション」リサーチライン用。

race_laps の各レース(race_id)について、
  1. race_num -> 標準発走時刻の近似表(POST_TIME_MAP)から発走推定時刻を求める
     (this_week_races.json 等の実データは当該週のみのため、過去分は近似表で統一。
      年度・季節による繰り上がりは本Phase 0では無視。Phase 1以降で必要なら
      season差分の補正を検討する)
  2. weather_obs から (date, venue) の発走推定時刻±30分の観測を抽出
  3. 風ベクトル平均(u=speed*sin(dir), v=speed*cos(dir) の平均から算出)で
     平均風速・平均風向を求める(単純な速度平均+方位角平均だと北0°/360°境界で
     破綻するため、気象学の標準手法であるベクトル平均を採用)
  4. venue_geo.straight_bearing_deg を使い、
       tail_home = 平均風速 * cos(radians((平均風向+180) - straight_bearing_deg))
     を計算(正 = 直線区間で追い風)
  5. gust_max = ウィンドウ内の最大瞬間風速の最大値

出力: race_wind テーブル (race_id, date, venue, race_num, post_time_est,
      wind_speed_avg, wind_dir_avg_deg, tail_home, gust_max, n_obs)

【重要】ここで使う係数(風向・風速平均の窓幅±30分、tail_home計算式)は
Phase 0の一発チェック用。後続フェーズで実運用に使う場合は年次WF再計算
(train/testの年を分離した係数再学習)が必須。
"""
import sqlite3
import math

DB_PATH = "keiba.db"

# race_num -> 標準発走時刻(近似, 通年平均的なJRA開催スケジュール)
# 実データ(this_week_races.json等のstart_time)は当該週のみのため、
# 過去分バックフィルでは全期間この近似表を統一使用する。
POST_TIME_MAP = {
    1: "09:55", 2: "10:25", 3: "10:55", 4: "11:25", 5: "11:55",
    6: "12:35", 7: "13:10", 8: "13:40", 9: "14:10", 10: "14:45",
    11: "15:20", 12: "16:25",
}


def hhmm_to_min(hhmm):
    h, m = hhmm.split(":")
    h, m = int(h), int(m)
    if h == 24:
        h = 0  # 24:00 -> 翌日0:00 相当だが同日窓内なので1440扱いにする
        return 1440
    return h * 60 + m


def build_race_wind(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS race_wind (
          race_id TEXT PRIMARY KEY,
          date TEXT, venue TEXT, race_num INTEGER,
          post_time_est TEXT,
          wind_speed_avg REAL, wind_dir_avg_deg REAL,
          tail_home REAL, gust_max REAL, n_obs INTEGER
        )
    """)
    conn.execute("DELETE FROM race_wind")

    bearings = {r[0]: r[1] for r in conn.execute(
        "SELECT venue, straight_bearing_deg FROM venue_geo")}

    races = conn.execute("""
        SELECT race_id, date, venue, race_num FROM race_laps
    """).fetchall()

    # weather_obs を (date,venue) 単位でまとめて読み込み、メモリ上でウィンドウ抽出
    # (2179日x場 x 144行/日 ≒ 30万行程度なので全読み込み可能)
    obs_by_dv = {}
    for row in conn.execute(
        "SELECT date, venue, hhmm, wind_speed, wind_dir_deg, gust_speed FROM weather_obs"
    ):
        date, venue, hhmm, wspd, wdir, gspd = row
        key = (date, venue)
        obs_by_dv.setdefault(key, []).append((hhmm, wspd, wdir, gspd))

    n_ok = n_no_post = n_no_obs = 0
    out_rows = []

    for race_id, date, venue, race_num in races:
        post_hhmm = POST_TIME_MAP.get(race_num)
        if post_hhmm is None or venue not in bearings:
            n_no_post += 1
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
                us.append(wspd * math.sin(rad))  # 東成分
                vs.append(wspd * math.cos(rad))  # 北成分
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

        bearing = bearings[venue]
        tail_home = wind_speed_avg * math.cos(
            math.radians((wind_dir_avg_deg + 180) - bearing))

        gust_max = max(gusts) if gusts else None

        out_rows.append((
            race_id, date, venue, race_num, post_hhmm,
            round(wind_speed_avg, 3), round(wind_dir_avg_deg, 1),
            round(tail_home, 3), gust_max, len(window),
        ))
        n_ok += 1

    conn.executemany("""
        INSERT OR REPLACE INTO race_wind
          (race_id, date, venue, race_num, post_time_est,
           wind_speed_avg, wind_dir_avg_deg, tail_home, gust_max, n_obs)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, out_rows)
    conn.commit()

    return {"total": len(races), "ok": n_ok, "no_post_or_venue": n_no_post,
            "no_obs": n_no_obs}


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    stats = build_race_wind(conn)
    print("race_wind construction stats:", stats)

    n = conn.execute("SELECT COUNT(*) FROM race_wind").fetchone()[0]
    print(f"race_wind rows: {n}")

    print("\nSample (tail_home distribution):")
    for r in conn.execute(
        "SELECT MIN(tail_home), AVG(tail_home), MAX(tail_home), "
        "AVG(wind_speed_avg), AVG(gust_max) FROM race_wind"
    ):
        print(f"  tail_home min/avg/max = {r[0]:.2f} / {r[1]:.2f} / {r[2]:.2f}, "
              f"avg wind_speed={r[3]:.2f}, avg gust_max={r[4]:.2f}")

    conn.close()


if __name__ == "__main__":
    main()
