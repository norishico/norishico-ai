"""
Phase 0b: 過去風データバックフィル
「風データ×展開シミュレーション」リサーチライン用。

results テーブルの開催日×場(2019-01-01以降、約2,179件)について、
気象庁「過去の気象データ検索」10分値ページをスクレイピングし、
weather_obs テーブルに格納する。

対象URL形式:
  https://www.data.jma.go.jp/obd/stats/etrn/view/{page_kind}.php
    ?prec_no={prec_no}&block_no={block_no:04d}&year=Y&month=M&day=D&view=
  page_kind は venue_geo.page_kind ('10min_s1' = 気象官署フル観測 / '10min_a1' = アメダス)
  で列構成が異なるため、page_kind ごとにパース列インデックスを切り替える。

エンコーディング: 実測の結果 charset=UTF-8 で正しくデコードできることを確認済み
  (メタタグ通りUTF-8。CLAUDE.mdのdividendsパイプラインのEUC-JP注意は別サイトの話)。

再開可能性: weather_obs に (date,venue) の行が1件でも存在すればその日はスキップする
  (INSERT OR IGNORE と合わせて二重の安全策)。

礼儀: 気象庁への配慮として各リクエスト間に time.sleep(1.5s) 以上を必ず挟む。
"""
import sqlite3
import time
import sys
import os
import re
import datetime
import requests
from bs4 import BeautifulSoup

DB_PATH = "keiba.db"
SLEEP_SEC = 1.6
TIMEOUT_SEC = 20
MAX_RETRY = 3
LOG_DIR = "logs"

WIND_DIR_DEG = {
    "北": 0.0, "北北東": 22.5, "北東": 45.0, "東北東": 67.5,
    "東": 90.0, "東南東": 112.5, "南東": 135.0, "南南東": 157.5,
    "南": 180.0, "南南西": 202.5, "南西": 225.0, "西南西": 247.5,
    "西": 270.0, "西北西": 292.5, "北西": 315.0, "北北西": 337.5,
    "静穏": None,  # 無風 = 方位なし
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) norishiko_ai research/1.0"}


def log(logf, msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    logf.write(line + "\n")
    logf.flush()


def parse_num(txt):
    txt = (txt or "").strip()
    if txt in ("", "///", "--", ")", "×"):
        return None
    # 値の末尾に ) が付く場合がある(推定値マーク)ので除去
    txt = txt.rstrip(")").rstrip("]")
    try:
        return float(txt)
    except ValueError:
        return None


def parse_wind_dir(txt):
    txt = (txt or "").strip()
    if txt in ("", "///", "--"):
        return None, None
    deg = WIND_DIR_DEG.get(txt)
    return txt, deg


def fetch_day(session, prec_no, block_no, page_kind, year, month, day):
    url = (f"https://www.data.jma.go.jp/obd/stats/etrn/view/{page_kind}.php"
           f"?prec_no={prec_no}&block_no={str(block_no).zfill(4)}"
           f"&year={year}&month={month:02d}&day={day:02d}&view=")
    resp = session.get(url, headers=HEADERS, timeout=TIMEOUT_SEC)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text, url


def parse_rows(html, page_kind):
    """10分値テーブルをパースして [(hhmm, precip, temp, humidity, wind_speed,
    wind_dir_txt, gust_speed, gust_dir_txt), ...] を返す"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="tablefix1")
    if table is None:
        return []

    out = []
    for tr in table.find_all("tr", class_="mtx"):
        tds = tr.find_all("td")
        if not tds:
            continue
        hhmm = tds[0].get_text(strip=True)
        if not re.match(r"^\d{2}:\d{2}$", hhmm):
            continue
        vals = [td.get_text(strip=True) for td in tds]

        if page_kind == "10min_s1":
            # [時刻,気圧現地,気圧海面,降水量,気温,湿度,風速,風向,最大瞬間風速,最大瞬間風向,日照]
            if len(vals) < 11:
                continue
            precip, temp = vals[3], vals[4]
            wind_speed, wind_dir = vals[6], vals[7]
            gust_speed, gust_dir = vals[8], vals[9]
        else:
            # 10min_a1: [時刻,降水量,気温,湿度,風速,風向,最大瞬間風速,最大瞬間風向,日照]
            if len(vals) < 9:
                continue
            precip, temp = vals[1], vals[2]
            wind_speed, wind_dir = vals[4], vals[5]
            gust_speed, gust_dir = vals[6], vals[7]

        out.append((hhmm, precip, temp, wind_speed, wind_dir, gust_speed, gust_dir))
    return out


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, f"fetch_weather_history_{ts}.log")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS weather_obs (
          date TEXT, venue TEXT, hhmm TEXT,
          wind_dir TEXT, wind_dir_deg REAL, wind_speed REAL,
          gust_speed REAL, gust_dir TEXT, temp REAL, precip REAL,
          source TEXT,
          PRIMARY KEY (date, venue, hhmm)
        )
    """)
    conn.commit()

    geo = {}
    for r in conn.execute("SELECT venue, prec_no, block_no, page_kind FROM venue_geo"):
        geo[r[0]] = (r[1], r[2], r[3])

    targets = conn.execute(
        "SELECT DISTINCT date, venue FROM results WHERE date>='2019-01-01' "
        "ORDER BY date, venue"
    ).fetchall()

    with open(log_path, "w", encoding="utf-8") as logf:
        log(logf, f"対象: {len(targets)} 件 (date x venue, 2019-01-01以降)")
        log(logf, f"venue_geo登録場: {list(geo.keys())}")

        session = requests.Session()
        n_done = n_skip = n_fail = n_rows = 0
        t0 = time.time()

        for i, (date, venue) in enumerate(targets):
            if venue not in geo:
                n_skip += 1
                if i % 200 == 0:
                    log(logf, f"[{i+1}/{len(targets)}] venue_geo未登録スキップ: {venue}")
                continue

            # 既にDBにあればスキップ(再開用)
            cnt = conn.execute(
                "SELECT COUNT(*) FROM weather_obs WHERE date=? AND venue=?", (date, venue)
            ).fetchone()[0]
            if cnt > 0:
                n_skip += 1
                continue

            prec_no, block_no, page_kind = geo[venue]
            try:
                y, m, d = date.split("-")
                y, m, d = int(y), int(m), int(d)
            except Exception:
                log(logf, f"[{i+1}/{len(targets)}] 日付パース失敗: {date}")
                n_fail += 1
                continue

            html, url = None, None
            for attempt in range(1, MAX_RETRY + 1):
                try:
                    html, url = fetch_day(session, prec_no, block_no, page_kind, y, m, d)
                    break
                except Exception as e:
                    log(logf, f"[{i+1}/{len(targets)}] fetch失敗(試行{attempt}) {date} {venue}: {e}")
                    time.sleep(SLEEP_SEC * attempt)

            if html is None:
                n_fail += 1
                time.sleep(SLEEP_SEC)
                continue

            rows = parse_rows(html, page_kind)
            insert_rows = []
            for (hhmm, precip, temp, wind_speed, wind_dir, gust_speed, gust_dir) in rows:
                wdir_txt, wdir_deg = parse_wind_dir(wind_dir)
                gdir_txt, _ = parse_wind_dir(gust_dir)
                insert_rows.append((
                    date, venue, hhmm,
                    wdir_txt, wdir_deg, parse_num(wind_speed),
                    parse_num(gust_speed), gdir_txt, parse_num(temp), parse_num(precip),
                    page_kind,
                ))

            if insert_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO weather_obs "
                    "(date, venue, hhmm, wind_dir, wind_dir_deg, wind_speed, "
                    " gust_speed, gust_dir, temp, precip, source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    insert_rows,
                )
                conn.commit()
                n_rows += len(insert_rows)
                n_done += 1
            else:
                log(logf, f"[{i+1}/{len(targets)}] 0行パース: {date} {venue} url={url}")
                n_fail += 1

            if (i + 1) % 50 == 0 or (i + 1) == len(targets):
                elapsed = time.time() - t0
                log(logf, f"[{i+1}/{len(targets)}] done={n_done} skip={n_skip} "
                          f"fail={n_fail} rows={n_rows} elapsed={elapsed/60:.1f}min")

            time.sleep(SLEEP_SEC)

        log(logf, f"完了: done={n_done} skip={n_skip} fail={n_fail} total_rows={n_rows}")

    conn.close()


if __name__ == "__main__":
    main()
