"""build_nar_db.py - 南関東競馬 過去成績DBを構築する
使い方:
  python nar/build_nar_db.py              # 2022-01-01〜昨日を全取得
  python nar/build_nar_db.py --since 2025-01-01
  python nar/build_nar_db.py --date 2026-04-17   # 特定日のみ
"""
import sys, os, re, time, sqlite3, argparse, requests
from datetime import date, timedelta
from pathlib import Path
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding='utf-8')

PROJ = Path(__file__).parent.parent
DB_PATH = PROJ / 'nar_keiba.db'

# 南関東4場のみ
NANKAN_VENUES = {
    '42': '浦和',
    '43': '船橋',
    '44': '大井',
    '45': '川崎',
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept-Language': 'ja,en-US;q=0.9',
    'Referer': 'https://nar.netkeiba.com/',
}

SLEEP = 0.35  # 秒間隔


# ── DB 初期化 ──────────────────────────────────────────────────
def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS nar_races (
        race_id     TEXT PRIMARY KEY,
        date        TEXT,
        venue_cd    TEXT,
        venue_name  TEXT,
        race_num    INTEGER,
        race_name   TEXT,
        class_code  TEXT,
        distance    INTEGER,
        track_type  TEXT,
        direction   TEXT,
        weather     TEXT,
        condition   TEXT,
        heads_count INTEGER
    );

    CREATE TABLE IF NOT EXISTS nar_results (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        race_id         TEXT,
        finish          INTEGER,
        waku            INTEGER,
        umaban          INTEGER,
        horse_name      TEXT,
        sex             TEXT,
        age             INTEGER,
        weight_carried  REAL,
        jockey          TEXT,
        stable          TEXT,
        time_str        TEXT,
        time_sec        REAL,
        margin          TEXT,
        popularity      INTEGER,
        odds            REAL,
        last_3f         REAL,
        body_weight     INTEGER,
        body_weight_diff INTEGER,
        UNIQUE(race_id, umaban)
    );

    CREATE TABLE IF NOT EXISTS nar_dividends (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        race_id     TEXT,
        bet_type    TEXT,
        combination TEXT,
        payout      INTEGER,
        pop_rank    INTEGER,
        UNIQUE(race_id, bet_type, combination)
    );

    CREATE INDEX IF NOT EXISTS idx_nar_results_race ON nar_results(race_id);
    CREATE INDEX IF NOT EXISTS idx_nar_results_horse ON nar_results(horse_name);
    CREATE INDEX IF NOT EXISTS idx_nar_races_date ON nar_races(date, venue_cd);
    """)
    conn.commit()


# ── ユーティリティ ────────────────────────────────────────────
def _response_encoding(r):
    """2026年にnar.netkeiba.comがEUC-JP→UTF-8へ切り替えたため、
    Content-Typeヘッダのcharsetを優先し、無ければ旧仕様のeuc-jpにフォールバックする"""
    ctype = r.headers.get('content-type', '')
    if 'charset=' in ctype.lower():
        return ctype.lower().split('charset=')[1].split(';')[0].strip()
    return 'euc-jp'


def fetch(url, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                return BeautifulSoup(r.content.decode(_response_encoding(r), errors='replace'), 'html.parser')
        except Exception as e:
            if attempt == retries - 1:
                print(f"  [ERROR] {url}: {e}", file=sys.stderr)
        time.sleep(1.0 * (attempt + 1))
    return None


def parse_time_sec(time_str):
    if not time_str:
        return None
    m = re.match(r'(\d+):(\d+\.\d+)', time_str)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    try:
        return float(time_str)
    except:
        return None


def parse_body_weight(text):
    m = re.match(r'(\d+)\(([+-]?\d+)\)', text.replace('−', '-'))
    if m:
        return int(m.group(1)), int(m.group(2))
    m2 = re.match(r'(\d+)', text)
    if m2:
        return int(m2.group(1)), None
    return None, None


def parse_race_data(soup, race_id):
    """RaceData01/02 からレース情報をパース"""
    venue_cd = race_id[4:6]
    venue_name = NANKAN_VENUES.get(venue_cd, '')
    race_num = int(race_id[10:12])
    date_str = race_id[6:10]
    date_fmt = f"{race_id[:4]}-{date_str[:2]}-{date_str[2:]}"

    d1 = soup.select_one('.RaceData01')
    d2 = soup.select_one('.RaceData02')
    d1_text = d1.get_text(' ', strip=True) if d1 else ''
    d2_text = d2.get_text(' ', strip=True) if d2 else ''

    # 距離・トラック・方向
    dist_m = re.search(r'([ダ芝]?)\s*(\d+)m\s*\(?\s*([右左])', d1_text)
    distance = int(dist_m.group(2)) if dist_m else None
    track_type = {'ダ': 'ダート', '芝': '芝', '': 'ダート'}.get(
        dist_m.group(1) if dist_m else '', 'ダート')
    direction = dist_m.group(3) if dist_m else None

    # 天候・馬場
    weather = re.search(r'天候:(\S+)', d1_text)
    condition = re.search(r'馬場:(\S+)', d1_text)

    # クラス
    class_m = re.search(r'([A-Z]\d|[A-Z]\d\d|Open|重賞|特別|新馬|特選|Jpn[123])', d2_text)
    class_code = class_m.group(1) if class_m else ''

    # 頭数
    heads_m = re.search(r'(\d+)頭', d2_text)
    heads_count = int(heads_m.group(1)) if heads_m else None

    # レース名
    title_el = soup.find('title')
    race_name = ''
    if title_el:
        t = title_el.get_text(strip=True)
        m = re.match(r'^(.+?)\s+結果', t)
        if m:
            race_name = m.group(1)

    return {
        'race_id': race_id,
        'date': date_fmt,
        'venue_cd': venue_cd,
        'venue_name': venue_name,
        'race_num': race_num,
        'race_name': race_name,
        'class_code': class_code,
        'distance': distance,
        'track_type': track_type,
        'direction': direction,
        'weather': weather.group(1) if weather else None,
        'condition': condition.group(1) if condition else None,
        'heads_count': heads_count,
    }


def parse_results(soup, race_id):
    rows = soup.select('table.RaceTable01 tr')
    results = []
    for row in rows:
        tds = row.find_all('td')
        if len(tds) < 10:
            continue
        try:
            finish_txt = tds[0].get_text(strip=True)
            finish = int(finish_txt) if finish_txt.isdigit() else None
            waku = int(tds[1].get_text(strip=True) or 0)
            umaban = int(tds[2].get_text(strip=True) or 0)
            horse_name = tds[3].get_text(strip=True)
            sex_age = tds[4].get_text(strip=True)
            sex = sex_age[0] if sex_age else ''
            age = int(sex_age[1:]) if len(sex_age) > 1 and sex_age[1:].isdigit() else None
            wt = tds[5].get_text(strip=True)
            weight_carried = float(wt) if wt else None
            jockey = tds[6].get_text(strip=True)
            time_str = tds[7].get_text(strip=True)
            margin = tds[8].get_text(strip=True)
            pop_txt = tds[9].get_text(strip=True)
            popularity = int(pop_txt) if pop_txt.isdigit() else None
            odds_txt = tds[10].get_text(strip=True)
            try:
                odds = float(odds_txt)
            except:
                odds = None
            last3f_txt = tds[11].get_text(strip=True) if len(tds) > 11 else ''
            try:
                last_3f = float(last3f_txt)
            except:
                last_3f = None
            stable = tds[12].get_text(strip=True) if len(tds) > 12 else ''
            bw_txt = tds[13].get_text(strip=True) if len(tds) > 13 else ''
            bw, bw_diff = parse_body_weight(bw_txt)

            results.append({
                'race_id': race_id,
                'finish': finish,
                'waku': waku,
                'umaban': umaban,
                'horse_name': horse_name,
                'sex': sex,
                'age': age,
                'weight_carried': weight_carried,
                'jockey': jockey,
                'stable': stable,
                'time_str': time_str,
                'time_sec': parse_time_sec(time_str),
                'margin': margin,
                'popularity': popularity,
                'odds': odds,
                'last_3f': last_3f,
                'body_weight': bw,
                'body_weight_diff': bw_diff,
            })
        except Exception as e:
            pass
    return results


def parse_dividends(soup, race_id):
    divs = []
    pay_tables = soup.select('.Payout_Detail_Table')
    bet_type_names = {
        '単勝': 'tansho', '複勝': 'fukusho', '枠連': 'wakuren',
        '馬連': 'umaren', '馬単': 'umatan', 'ワイド': 'wide',
        '三連複': 'sanrenpuku', '三連単': 'sanrentan',
        '3連複': 'sanrenpuku', '3連単': 'sanrentan',
    }
    # 複勝は1着分ごとに単一馬番、ワイドは1組ごとに馬番ペアが
    # <br>区切りで同一セル内に並ぶため、区切り文字を保持して展開する
    MULTI_SINGLE = {'複勝'}
    MULTI_PAIR = {'ワイド'}
    for tbl in pay_tables:
        rows = tbl.select('tr')
        for row in rows:
            cells = row.find_all(['td', 'th'])
            if len(cells) < 3:
                continue
            bet_type_jp = cells[0].get_text(strip=True)
            bet_type = bet_type_names.get(bet_type_jp, bet_type_jp)

            if bet_type_jp in MULTI_SINGLE or bet_type_jp in MULTI_PAIR:
                combo_parts = [p for p in cells[1].get_text(separator='|', strip=True).split('|') if p]
                payout_parts = [p for p in cells[2].get_text(separator='|', strip=True).split('|') if p]
                pop_parts = [p for p in cells[3].get_text(separator='|', strip=True).split('|') if p] if len(cells) > 3 else []
                if bet_type_jp in MULTI_PAIR:
                    combo_parts = ['-'.join(combo_parts[i:i + 2]) for i in range(0, len(combo_parts), 2)]
                if len(combo_parts) != len(payout_parts):
                    # セル構造が想定と異なる（フォーマット変更等）→ 破損データを残さずスキップ
                    continue
                for i, combo_text in enumerate(combo_parts):
                    try:
                        payout = int(re.sub(r'[^\d]', '', payout_parts[i].split('円')[0]))
                        pop_m = re.search(r'(\d+)人気', pop_parts[i]) if i < len(pop_parts) else None
                        divs.append({
                            'race_id': race_id,
                            'bet_type': bet_type,
                            'combination': combo_text,
                            'payout': payout,
                            'pop_rank': int(pop_m.group(1)) if pop_m else None,
                        })
                    except:
                        pass
                continue

            combo_text = cells[1].get_text(strip=True)
            payout_text = cells[2].get_text(strip=True)
            try:
                payout = int(re.sub(r'[^\d]', '', payout_text.split('円')[0]))
                pop_m = re.search(r'(\d+)人気', cells[3].get_text(strip=True) if len(cells) > 3 else '')
                pop_rank = int(pop_m.group(1)) if pop_m else None
                divs.append({
                    'race_id': race_id,
                    'bet_type': bet_type,
                    'combination': combo_text,
                    'payout': payout,
                    'pop_rank': pop_rank,
                })
            except:
                pass
    return divs


# ── 日付別取得 ────────────────────────────────────────────────
def get_race_ids_for_date(date_str: str):
    """date_str='20260417' → 南関東のrace_idリスト"""
    url = f"https://nar.netkeiba.com/top/race_list_sub.html?kaisai_date={date_str}"
    soup = fetch(url)
    if not soup:
        return []
    links = soup.find_all('a', href=True)
    rids = set()
    for a in links:
        h = a['href']
        if 'race_id=' in h:
            rid = h.split('race_id=')[1].split('&')[0].split('#')[0]
            if len(rid) == 12 and rid[4:6] in NANKAN_VENUES:
                rids.add(rid)
    return sorted(rids)


def process_race(conn, race_id, verbose=False):
    """1レース取得→DB保存。既存ならスキップ"""
    exists = conn.execute("SELECT 1 FROM nar_races WHERE race_id=?", (race_id,)).fetchone()
    if exists:
        return 'skip'

    url = f"https://nar.netkeiba.com/race/result.html?race_id={race_id}"
    soup = fetch(url)
    if not soup:
        return 'error'

    # 結果テーブルが空なら未確定
    rows = soup.select('table.RaceTable01 tr')
    data_rows = [r for r in rows if r.find_all('td')]
    if not data_rows:
        return 'no_data'

    race_info = parse_race_data(soup, race_id)
    results = parse_results(soup, race_id)
    dividends = parse_dividends(soup, race_id)

    if not results:
        return 'no_data'

    race_info['heads_count'] = race_info['heads_count'] or len(results)

    conn.execute("""
        INSERT OR IGNORE INTO nar_races
        (race_id,date,venue_cd,venue_name,race_num,race_name,class_code,
         distance,track_type,direction,weather,condition,heads_count)
        VALUES(:race_id,:date,:venue_cd,:venue_name,:race_num,:race_name,:class_code,
               :distance,:track_type,:direction,:weather,:condition,:heads_count)
    """, race_info)

    conn.executemany("""
        INSERT OR IGNORE INTO nar_results
        (race_id,finish,waku,umaban,horse_name,sex,age,weight_carried,jockey,stable,
         time_str,time_sec,margin,popularity,odds,last_3f,body_weight,body_weight_diff)
        VALUES(:race_id,:finish,:waku,:umaban,:horse_name,:sex,:age,:weight_carried,
               :jockey,:stable,:time_str,:time_sec,:margin,:popularity,:odds,
               :last_3f,:body_weight,:body_weight_diff)
    """, results)

    conn.executemany("""
        INSERT OR IGNORE INTO nar_dividends
        (race_id,bet_type,combination,payout,pop_rank)
        VALUES(:race_id,:bet_type,:combination,:payout,:pop_rank)
    """, dividends)

    conn.commit()
    if verbose:
        r = race_info
        print(f"  ✓ {race_id} {r['venue_name']}{r['race_num']}R {r['class_code']} "
              f"{r['distance']}m {len(results)}頭 払戻{len(dividends)}件")
    return 'ok'


# ── メイン ────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--since', default='2022-01-01')
    parser.add_argument('--until', default=str(date.today() - timedelta(days=1)))
    parser.add_argument('--date', help='特定日のみ (YYYY-MM-DD)')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    init_db(conn)

    if args.date:
        dates = [args.date.replace('-', '')]
    else:
        since = date.fromisoformat(args.since)
        until = date.fromisoformat(args.until)
        d = since
        dates = []
        while d <= until:
            dates.append(d.strftime('%Y%m%d'))
            d += timedelta(days=1)

    total_ok = total_skip = total_err = 0
    print(f"取得期間: {dates[0]} 〜 {dates[-1]} ({len(dates)}日)")
    print(f"対象: 浦和(42)/船橋(43)/大井(44)/川崎(45)")
    print(f"保存先: {DB_PATH}")
    print()

    for i, d_str in enumerate(dates):
        race_ids = get_race_ids_for_date(d_str)
        nankan = [r for r in race_ids if r[4:6] in NANKAN_VENUES]
        if not nankan:
            if args.verbose:
                print(f"{d_str}: 南関東レースなし")
            time.sleep(SLEEP)
            continue

        print(f"{d_str}: {len(nankan)}R取得中...", end=' ', flush=True)
        ok = skip = err = 0
        for rid in nankan:
            status = process_race(conn, rid, verbose=args.verbose)
            if status == 'ok':
                ok += 1
            elif status == 'skip':
                skip += 1
            else:
                err += 1
            time.sleep(SLEEP)

        print(f"OK={ok} skip={skip} err={err}")
        total_ok += ok
        total_skip += skip
        total_err += err

        # 進捗表示 (100日ごと)
        if (i + 1) % 100 == 0:
            cnt = conn.execute("SELECT COUNT(*) FROM nar_races").fetchone()[0]
            print(f"  [進捗] {i+1}/{len(dates)}日処理済 DB総レース数={cnt}")

    conn.close()
    cnt_r = sqlite3.connect(DB_PATH).execute("SELECT COUNT(*) FROM nar_races").fetchone()[0]
    cnt_h = sqlite3.connect(DB_PATH).execute("SELECT COUNT(*) FROM nar_results").fetchone()[0]
    print(f"\n完了: OK={total_ok} skip={total_skip} err={total_err}")
    print(f"DB: nar_races={cnt_r}件 nar_results={cnt_h}件")


if __name__ == '__main__':
    main()
