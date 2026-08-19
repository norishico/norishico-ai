"""backfill_laps_corner_pedigree.py - 委員会承認(2026-08-19)に基づき、
地方競馬公式サイト(keiba.go.jp)の月次CSVからラップタイム・コーナー通過順・
血統(公式版)を新規テーブルに取り込む。

新規テーブルのみ(nar_laps / nar_corner_pos / nar_pedigree_official)。
既存テーブル(nar_races/nar_results/nar_dividends/nar_horse_pedigree等)には
一切触れない。INSERT OR IGNOREのみ。

使い方:
  python nar/backfill_laps_corner_pedigree.py --dry-run
  python nar/backfill_laps_corner_pedigree.py --apply
  python nar/backfill_laps_corner_pedigree.py --apply --start 2026-01 --end 2026-08
"""
import argparse
import csv
import io
import sqlite3
import time
import urllib.request
import zipfile
from pathlib import Path

PROJ = Path(__file__).parent.parent
DB_PATH = PROJ / 'nar_keiba.db'
CACHE_DIR = PROJ.parent / 'AppData_UNUSED'  # not used; CSV kept in-memory only

VENUE_CODE = {'浦和': '42', '船橋': '43', '大井': '44', '川崎': '45'}


def race_id_from_csv(date8, venue_name, race_num):
    cd = VENUE_CODE.get(venue_name)
    if cd is None:
        return None
    return f"{date8[0:4]}{cd}{date8[4:6]}{date8[6:8]}{int(race_num):02d}"


def month_range(start_ym, end_ym):
    y, m = start_ym
    ey, em = end_ym
    out = []
    while (y, m) <= (ey, em):
        out.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def download_month(year, month, retries=3):
    url = (f"https://www.keiba.go.jp/KeibaWeb/DataDownload/RaceDataDownload"
           f"?type=monthly&k_year={year}&k_month={month}")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as e:
            if attempt == retries - 1:
                print(f"  DOWNLOAD FAILED {year}-{month:02d}: {e}")
                return None
            time.sleep(2 * (attempt + 1))
    return None


def read_csv_from_zip(zip_bytes, suffix):
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    name = next(n for n in zf.namelist() if n.endswith(suffix))
    with zf.open(name) as f:
        text = f.read().decode('utf-8-sig')
    return list(csv.DictReader(io.StringIO(text)))


def to_float(s):
    s = (s or '').strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def init_tables(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS nar_laps (
        race_id TEXT PRIMARY KEY,
        agari_4f REAL, agari_3f REAL,
        lap1 REAL, lap2 REAL, lap3 REAL, lap4 REAL, lap5 REAL,
        lap6 REAL, lap7 REAL, lap8 REAL, lap9 REAL, lap10 REAL,
        lap11 REAL, lap12 REAL, lap13 REAL, lap14 REAL, lap15 REAL
    );
    CREATE TABLE IF NOT EXISTS nar_corner_pos (
        race_id TEXT, corner_num INTEGER, corner_name TEXT, passing_order TEXT,
        PRIMARY KEY(race_id, corner_num)
    );
    CREATE TABLE IF NOT EXISTS nar_pedigree_official (
        horse_name TEXT PRIMARY KEY, sire TEXT, dam TEXT, dam_sire TEXT
    );
    """)
    conn.commit()


def process_racelist(rows):
    laps, corners = [], []
    for row in rows:
        venue = row['競馬場']
        if venue not in VENUE_CODE:
            continue
        rid = race_id_from_csv(row['競走年月日'], venue, row['レース番号'])
        if rid is None:
            continue
        lap_vals = [to_float(row.get(f'ハロンタイム{i}')) for i in range(1, 16)]
        laps.append((rid, to_float(row.get('上がり4F')), to_float(row.get('上がり3F')), *lap_vals))
        for i in range(1, 9):
            cname = (row.get(f'コーナー名称{i}') or '').strip()
            cpos = (row.get(f'コーナー通過順{i}') or '').strip()
            if cname or cpos:
                corners.append((rid, i, cname, cpos))
    return laps, corners


def process_horselist(rows):
    ped = {}
    for row in rows:
        venue = row['競馬場']
        if venue not in VENUE_CODE:
            continue
        name = (row.get('馬名') or '').strip()
        if not name or name in ped:
            continue
        ped[name] = (name, (row.get('父馬名') or '').strip() or None,
                      (row.get('母馬名') or '').strip() or None,
                      (row.get('母父馬名') or '').strip() or None)
    return list(ped.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2022-01')
    ap.add_argument('--end', default='2026-08')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    sy, sm = (int(x) for x in args.start.split('-'))
    ey, em = (int(x) for x in args.end.split('-'))
    months = month_range((sy, sm), (ey, em))
    print(f"対象月数: {len(months)} ({args.start} 〜 {args.end})")

    if not args.apply:
        print("dry-run: --apply で実投入")
        return

    backup_path = (PROJ.parent / 'AppData_UNUSED')  # placeholder, real backup done by caller
    conn = sqlite3.connect(str(DB_PATH))
    init_tables(conn)

    total_laps = total_corners = total_ped = 0
    year_stats = {}
    for (y, m) in months:
        data = download_month(y, m)
        if data is None:
            continue
        try:
            race_rows = read_csv_from_zip(data, 'racelist.csv')
            horse_rows = read_csv_from_zip(data, 'horselist.csv')
        except Exception as e:
            print(f"  PARSE FAILED {y}-{m:02d}: {e}")
            continue

        laps, corners = process_racelist(race_rows)
        ped = process_horselist(horse_rows)

        cur = conn.cursor()
        cur.executemany(
            """INSERT OR IGNORE INTO nar_laps
               (race_id,agari_4f,agari_3f,lap1,lap2,lap3,lap4,lap5,lap6,lap7,lap8,
                lap9,lap10,lap11,lap12,lap13,lap14,lap15)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            laps)
        cur.executemany(
            "INSERT OR IGNORE INTO nar_corner_pos (race_id,corner_num,corner_name,passing_order) VALUES (?,?,?,?)",
            corners)
        cur.executemany(
            "INSERT OR IGNORE INTO nar_pedigree_official (horse_name,sire,dam,dam_sire) VALUES (?,?,?,?)",
            ped)
        conn.commit()

        total_laps += len(laps)
        total_corners += len(corners)
        total_ped += len(ped)
        year_stats.setdefault(y, [0, 0, 0])
        year_stats[y][0] += len(laps)
        year_stats[y][1] += len(corners)
        year_stats[y][2] += len(ped)
        print(f"  {y}-{m:02d}: laps+{len(laps)} corners+{len(corners)} ped+{len(ped)}")
        time.sleep(1.0)

    print(f"\n合計: nar_laps={total_laps} nar_corner_pos={total_corners} nar_pedigree_official(累積ユニーク挿入試行)={total_ped}")
    for y in sorted(year_stats):
        l, c, p = year_stats[y]
        print(f"  {y}: laps={l} corners={c} ped_rows_seen={p}")
    conn.close()


if __name__ == '__main__':
    main()
