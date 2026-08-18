"""build_nar_db_fast.py - 南関東競馬 過去成績DB高速構築 (並列取得)
5並列 + SQLite write-queue で ~3倍高速化
使い方:
  python nar/build_nar_db_fast.py              # 2022-01-01〜昨日
  python nar/build_nar_db_fast.py --since 2022-01-01 --workers 5
"""
import sys, os, re, time, sqlite3, argparse, requests, queue, threading
from datetime import date, timedelta
from pathlib import Path
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding='utf-8')

PROJ = Path(__file__).parent.parent
DB_PATH = PROJ / 'nar_keiba.db'

NANKAN_VENUES = {'42': '浦和', '43': '船橋', '44': '大井', '45': '川崎'}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept-Language': 'ja,en-US;q=0.9',
    'Referer': 'https://nar.netkeiba.com/',
}

SLEEP = 0.5  # IP制限対策 (0.2s→0.5s, 2026-04-24更新)


def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS nar_races (
        race_id TEXT PRIMARY KEY, date TEXT, venue_cd TEXT, venue_name TEXT,
        race_num INTEGER, race_name TEXT, class_code TEXT, distance INTEGER,
        track_type TEXT, direction TEXT, weather TEXT, condition TEXT, heads_count INTEGER
    );
    CREATE TABLE IF NOT EXISTS nar_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        race_id TEXT, finish INTEGER, waku INTEGER, umaban INTEGER,
        horse_name TEXT, sex TEXT, age INTEGER, weight_carried REAL,
        jockey TEXT, stable TEXT, time_str TEXT, time_sec REAL, margin TEXT,
        popularity INTEGER, odds REAL, last_3f REAL, body_weight INTEGER,
        body_weight_diff INTEGER,
        UNIQUE(race_id, umaban)
    );
    CREATE TABLE IF NOT EXISTS nar_dividends (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        race_id TEXT, bet_type TEXT, combination TEXT, payout INTEGER, pop_rank INTEGER,
        UNIQUE(race_id, bet_type, combination)
    );
    CREATE INDEX IF NOT EXISTS idx_nar_results_race ON nar_results(race_id);
    CREATE INDEX IF NOT EXISTS idx_nar_results_horse ON nar_results(horse_name);
    CREATE INDEX IF NOT EXISTS idx_nar_races_date ON nar_races(date, venue_cd);
    """)
    conn.commit()


def _response_encoding(r):
    """2026年にnar.netkeiba.comがEUC-JP→UTF-8へ切り替えたため、
    Content-Typeヘッダのcharsetを優先し、無ければ旧仕様のeuc-jpにフォールバックする"""
    ctype = r.headers.get('content-type', '')
    if 'charset=' in ctype.lower():
        return ctype.lower().split('charset=')[1].split(';')[0].strip()
    return 'euc-jp'


def fetch_raw(url, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                return BeautifulSoup(r.content.decode(_response_encoding(r), errors='replace'), 'html.parser')
        except Exception:
            pass
        time.sleep(0.8 * (attempt + 1))
    return None


def parse_time_sec(t):
    m = re.match(r'(\d+):(\d+\.\d+)', t or '')
    return int(m.group(1)) * 60 + float(m.group(2)) if m else None


def parse_body_weight(text):
    m = re.match(r'(\d+)\(([+-]?\d+)\)', (text or '').replace('−', '-'))
    if m:
        return int(m.group(1)), int(m.group(2))
    m2 = re.match(r'(\d+)', text or '')
    return (int(m2.group(1)), None) if m2 else (None, None)


def fetch_race_data(race_id):
    """1レースのデータを取得してdict返す。失敗=None"""
    soup = fetch_raw(f"https://nar.netkeiba.com/race/result.html?race_id={race_id}")
    if not soup:
        return None

    rows = soup.select('table.RaceTable01 tr')
    data_rows = [r for r in rows if r.find_all('td')]
    if not data_rows:
        return None

    venue_cd = race_id[4:6]
    ds = race_id[6:10]
    date_fmt = f"{race_id[:4]}-{ds[:2]}-{ds[2:]}"

    d1 = soup.select_one('.RaceData01')
    d2 = soup.select_one('.RaceData02')
    d1t = d1.get_text(' ', strip=True) if d1 else ''
    d2t = d2.get_text(' ', strip=True) if d2 else ''

    dm = re.search(r'([ダ芝]?)\s*(\d+)m\s*\(?\s*([右左])', d1t)
    wm = re.search(r'天候:(\S+)', d1t)
    cm = re.search(r'馬場:(\S+)', d1t)
    clsm = re.search(r'([A-Z]\d+|Open|重賞|特別|新馬|特選|Jpn[123])', d2t)
    hm = re.search(r'(\d+)頭', d2t)
    tt = soup.find('title')
    race_name = ''
    if tt:
        m = re.match(r'^(.+?)\s+結果', tt.get_text(strip=True))
        if m:
            race_name = m.group(1)

    race_info = {
        'race_id': race_id,
        'date': date_fmt,
        'venue_cd': venue_cd,
        'venue_name': NANKAN_VENUES.get(venue_cd, ''),
        'race_num': int(race_id[10:12]),
        'race_name': race_name,
        'class_code': clsm.group(1) if clsm else '',
        'distance': int(dm.group(2)) if dm else None,
        'track_type': {'ダ': 'ダート', '芝': '芝', '': 'ダート'}.get(dm.group(1) if dm else '', 'ダート'),
        'direction': dm.group(3) if dm else None,
        'weather': wm.group(1) if wm else None,
        'condition': cm.group(1) if cm else None,
        'heads_count': int(hm.group(1)) if hm else None,
    }

    results = []
    for row in data_rows:
        tds = row.find_all('td')
        if len(tds) < 10:
            continue
        try:
            ft = tds[0].get_text(strip=True)
            finish = int(ft) if ft.isdigit() else None
            sex_age = tds[4].get_text(strip=True)
            odds_txt = tds[10].get_text(strip=True)
            bw_txt = tds[13].get_text(strip=True) if len(tds) > 13 else ''
            bw, bwd = parse_body_weight(bw_txt)
            try:
                odds = float(odds_txt)
            except:
                odds = None
            try:
                last_3f = float(tds[11].get_text(strip=True)) if len(tds) > 11 else None
            except:
                last_3f = None
            results.append({
                'race_id': race_id,
                'finish': finish,
                'waku': int(tds[1].get_text(strip=True) or 0),
                'umaban': int(tds[2].get_text(strip=True) or 0),
                'horse_name': tds[3].get_text(strip=True),
                'sex': sex_age[0] if sex_age else '',
                'age': int(sex_age[1:]) if len(sex_age) > 1 and sex_age[1:].isdigit() else None,
                'weight_carried': (lambda x: float(x) if x else None)(tds[5].get_text(strip=True)),
                'jockey': tds[6].get_text(strip=True),
                'stable': tds[12].get_text(strip=True) if len(tds) > 12 else '',
                'time_str': tds[7].get_text(strip=True),
                'time_sec': parse_time_sec(tds[7].get_text(strip=True)),
                'margin': tds[8].get_text(strip=True),
                'popularity': (lambda x: int(x) if x.isdigit() else None)(tds[9].get_text(strip=True)),
                'odds': odds,
                'last_3f': last_3f,
                'body_weight': bw,
                'body_weight_diff': bwd,
            })
        except Exception:
            pass

    # 払戻
    bet_names = {'単勝': 'tansho', '複勝': 'fukusho', '枠連': 'wakuren',
                 '馬連': 'umaren', '馬単': 'umatan', 'ワイド': 'wide',
                 '三連複': 'sanrenpuku', '三連単': 'sanrentan',
                 '3連複': 'sanrenpuku', '3連単': 'sanrentan'}
    # 複勝は1着分ごとに単一馬番、ワイドは1組ごとに馬番ペアが
    # <br>区切りで同一セル内に並ぶため、区切り文字を保持して展開する
    MULTI_SINGLE = {'複勝'}
    MULTI_PAIR = {'ワイド'}
    dividends = []
    for tbl in soup.select('.Payout_Detail_Table'):
        for row in tbl.select('tr'):
            cells = row.find_all(['td', 'th'])
            if len(cells) < 3:
                continue
            bt_jp = cells[0].get_text(strip=True)
            bt = bet_names.get(bt_jp, bt_jp)

            if bt_jp in MULTI_SINGLE or bt_jp in MULTI_PAIR:
                combo_parts = [p for p in cells[1].get_text(separator='|', strip=True).split('|') if p]
                payout_parts = [p for p in cells[2].get_text(separator='|', strip=True).split('|') if p]
                pop_parts = [p for p in cells[3].get_text(separator='|', strip=True).split('|') if p] if len(cells) > 3 else []
                if bt_jp in MULTI_PAIR:
                    combo_parts = ['-'.join(combo_parts[i:i + 2]) for i in range(0, len(combo_parts), 2)]
                if len(combo_parts) != len(payout_parts):
                    continue
                for i, combo in enumerate(combo_parts):
                    try:
                        payout = int(re.sub(r'[^\d]', '', payout_parts[i].split('円')[0]))
                        pm = re.search(r'(\d+)人気', pop_parts[i]) if i < len(pop_parts) else None
                        dividends.append({
                            'race_id': race_id,
                            'bet_type': bt,
                            'combination': combo,
                            'payout': payout,
                            'pop_rank': int(pm.group(1)) if pm else None,
                        })
                    except Exception:
                        pass
                continue

            combo = cells[1].get_text(strip=True)
            pay_txt = cells[2].get_text(strip=True)
            try:
                payout = int(re.sub(r'[^\d]', '', pay_txt.split('円')[0]))
                pm = re.search(r'(\d+)人気', cells[3].get_text(strip=True) if len(cells) > 3 else '')
                dividends.append({
                    'race_id': race_id,
                    'bet_type': bt,
                    'combination': combo,
                    'payout': payout,
                    'pop_rank': int(pm.group(1)) if pm else None,
                })
            except Exception:
                pass

    race_info['heads_count'] = race_info['heads_count'] or len(results)
    return {'race': race_info, 'results': results, 'dividends': dividends}


def get_race_ids_for_date(date_str):
    soup = fetch_raw(f"https://nar.netkeiba.com/top/race_list_sub.html?kaisai_date={date_str}")
    if not soup:
        return []
    rids = set()
    for a in soup.find_all('a', href=True):
        h = a['href']
        if 'race_id=' in h:
            rid = h.split('race_id=')[1].split('&')[0].split('#')[0]
            if len(rid) == 12 and rid[4:6] in NANKAN_VENUES:
                rids.add(rid)
    return sorted(rids)


def write_worker(write_q, db_path):
    """専用writeスレッド: キューからデータを受け取りDBに書き込む"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    init_db(conn)
    while True:
        item = write_q.get()
        if item is None:
            break
        try:
            d = item
            conn.execute("""
                INSERT OR IGNORE INTO nar_races
                (race_id,date,venue_cd,venue_name,race_num,race_name,class_code,
                 distance,track_type,direction,weather,condition,heads_count)
                VALUES(:race_id,:date,:venue_cd,:venue_name,:race_num,:race_name,:class_code,
                       :distance,:track_type,:direction,:weather,:condition,:heads_count)
            """, d['race'])
            conn.executemany("""
                INSERT OR IGNORE INTO nar_results
                (race_id,finish,waku,umaban,horse_name,sex,age,weight_carried,jockey,stable,
                 time_str,time_sec,margin,popularity,odds,last_3f,body_weight,body_weight_diff)
                VALUES(:race_id,:finish,:waku,:umaban,:horse_name,:sex,:age,:weight_carried,
                       :jockey,:stable,:time_str,:time_sec,:margin,:popularity,:odds,
                       :last_3f,:body_weight,:body_weight_diff)
            """, d['results'])
            conn.executemany("""
                INSERT OR IGNORE INTO nar_dividends
                (race_id,bet_type,combination,payout,pop_rank)
                VALUES(:race_id,:bet_type,:combination,:payout,:pop_rank)
            """, d['dividends'])
            conn.commit()
        except Exception as e:
            print(f"  [WRITE ERROR] {e}", file=sys.stderr)
        finally:
            write_q.task_done()
    conn.close()


def fetch_worker(race_id, existing_ids):
    """並列fetchタスク"""
    if race_id in existing_ids:
        return race_id, 'skip', None
    time.sleep(SLEEP)
    data = fetch_race_data(race_id)
    if data is None:
        return race_id, 'no_data', None
    return race_id, 'ok', data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--since', default='2022-01-01')
    parser.add_argument('--until', default=str(date.today() - timedelta(days=1)))
    parser.add_argument('--workers', type=int, default=5)
    args = parser.parse_args()

    # 既存race_idセットを読み込む
    conn_r = sqlite3.connect(DB_PATH)
    init_db(conn_r)
    existing_ids = set(r[0] for r in conn_r.execute("SELECT race_id FROM nar_races").fetchall())
    conn_r.close()
    print(f"既存: {len(existing_ids)}件 → resumeします")

    # write queue + worker thread
    write_q = queue.Queue(maxsize=200)
    wt = threading.Thread(target=write_worker, args=(write_q, DB_PATH), daemon=True)
    wt.start()

    since = date.fromisoformat(args.since)
    until = date.fromisoformat(args.until)
    d = since
    dates = []
    while d <= until:
        dates.append(d.strftime('%Y%m%d'))
        d += timedelta(days=1)

    print(f"期間: {dates[0]} 〜 {dates[-1]} ({len(dates)}日) workers={args.workers}")

    total_ok = total_skip = total_err = 0
    checkpoint = 0

    # 日付ループ (race_list取得は逐次、race page取得は並列)
    for i, d_str in enumerate(dates):
        race_ids = get_race_ids_for_date(d_str)
        nankan = [r for r in race_ids if r[4:6] in NANKAN_VENUES]
        if not nankan:
            continue

        new_ids = [r for r in nankan if r not in existing_ids]
        if not new_ids:
            total_skip += len(nankan)
            continue

        print(f"{d_str}: {len(new_ids)}R取得...", end=' ', flush=True)
        ok = err = 0

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(fetch_worker, rid, existing_ids): rid for rid in new_ids}
            for fut in as_completed(futures):
                rid, status, data = fut.result()
                if status == 'ok' and data:
                    write_q.put(data)
                    existing_ids.add(rid)
                    ok += 1
                elif status == 'skip':
                    total_skip += 1
                else:
                    err += 1

        print(f"OK={ok} err={err}")
        total_ok += ok
        total_err += err

        checkpoint += 1
        if checkpoint % 50 == 0:
            # 進捗
            n = sqlite3.connect(DB_PATH).execute("SELECT COUNT(*) FROM nar_races").fetchone()[0]
            print(f"  [進捗] {i+1}/{len(dates)}日 DB={n}件")

    # write queueが空になるまで待つ
    write_q.put(None)  # 終了シグナル
    wt.join()

    n_r = sqlite3.connect(DB_PATH).execute("SELECT COUNT(*) FROM nar_races").fetchone()[0]
    n_h = sqlite3.connect(DB_PATH).execute("SELECT COUNT(*) FROM nar_results").fetchone()[0]
    print(f"\n完了: OK={total_ok} skip={total_skip} err={total_err}")
    print(f"DB: nar_races={n_r}件 nar_results={n_h}件")


if __name__ == '__main__':
    main()
