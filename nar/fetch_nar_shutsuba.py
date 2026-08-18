"""fetch_nar_shutsuba.py - 南関東 今週の出馬表取得
使い方:
  python nar/fetch_nar_shutsuba.py                    # 本日の出馬表
  python nar/fetch_nar_shutsuba.py --date 2026-04-26  # 指定日
  python nar/fetch_nar_shutsuba.py --days 3           # 今日から3日分
"""
import sys, re, time, json, argparse, requests, sqlite3
from datetime import date, timedelta
from pathlib import Path
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding='utf-8')

PROJ = Path(__file__).parent.parent
OUT_PATH = PROJ / 'nar_shutsuba.json'

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

SLEEP = 0.3
DB_PATH = PROJ / 'nar_keiba.db'


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
                return BeautifulSoup(r.content, 'html.parser', from_encoding=_response_encoding(r))
        except Exception as e:
            if attempt == retries - 1:
                print(f"  [ERROR] {url}: {e}", file=sys.stderr)
        time.sleep(1.0 * (attempt + 1))
    return None


def get_race_ids_for_date(date_str: str):
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


def parse_shutsuba(soup, race_id):
    venue_cd = race_id[4:6]
    venue_name = NANKAN_VENUES.get(venue_cd, '')
    race_num = int(race_id[10:12])
    ds = race_id[6:10]
    date_str = f"{race_id[:4]}-{ds[:2]}-{ds[2:]}"

    d1 = soup.select_one('.RaceData01')
    d2 = soup.select_one('.RaceData02')
    d1_text = d1.get_text(' ', strip=True) if d1 else ''
    d2_text = d2.get_text(' ', strip=True) if d2 else ''

    # 距離・馬場
    dist_m = re.search(r'([ダ芝]?)\s*(\d+)m\s*\(?\s*([右左])', d1_text)
    distance = int(dist_m.group(2)) if dist_m else None
    track_type = {'ダ': 'ダート', '芝': '芝', ''  : 'ダート'}.get(
        dist_m.group(1) if dist_m else '', 'ダート')
    direction = dist_m.group(3) if dist_m else None
    weather_m = re.search(r'天候:(\S+)', d1_text)
    cond_m = re.search(r'馬場:(\S+)', d1_text)
    start_time_m = re.search(r'(\d+:\d+)発走', d1_text)

    class_m = re.search(r'([A-Z]\d+|Open|重賞|特別|新馬|特選|Jpn[123])', d2_text)
    class_code = class_m.group(1) if class_m else ''
    heads_m = re.search(r'(\d+)頭', d2_text)

    title_el = soup.find('title')
    race_name = ''
    if title_el:
        t = title_el.get_text(strip=True)
        m = re.match(r'^(.+?)\s+出馬表', t)
        if m:
            race_name = m.group(1)
        else:
            race_name = d2_text[:30] if d2_text else ''

    entries = []
    rows = soup.select('table tr.HorseList')
    for row in rows:
        tds = row.find_all('td')
        if len(tds) < 8:
            continue
        try:
            waku = int(tds[0].get_text(strip=True) or 0)
            umaban = int(tds[1].get_text(strip=True) or 0)
            horse_name = tds[3].get_text(strip=True)
            sex_age = tds[4].get_text(strip=True)
            sex = sex_age[0] if sex_age else ''
            age_str = sex_age[1:] if len(sex_age) > 1 else ''
            age = int(age_str) if age_str.isdigit() else None
            wt = tds[5].get_text(strip=True)
            weight_carried = float(wt) if wt else None
            jockey = tds[6].get_text(strip=True)
            trainer_raw = tds[7].get_text(strip=True) if len(tds) > 7 else ''
            # trainer: "浦和平山真希" → venue prefix を除く
            stable_venue = venue_name
            trainer = trainer_raw.replace(stable_venue, '') if stable_venue else trainer_raw

            # body_weight: td[8] "441(0)68.07" → 441, 0
            bw_text = tds[8].get_text(strip=True) if len(tds) > 8 else ''
            bw_m = re.match(r'(\d+)\(([+-]?\d+)\)', bw_text.replace('−', '-'))
            body_weight = int(bw_m.group(1)) if bw_m else None
            bw_diff = int(bw_m.group(2)) if bw_m else None

            # odds: td[9] (Popular)
            odds_txt = tds[9].get_text(strip=True) if len(tds) > 9 else ''
            try:
                odds = float(re.sub(r'[^\d.]', '', odds_txt))
            except:
                odds = None

            # horse_id (db.netkeiba.com リンクから取得)
            a_horse = row.find('a', href=re.compile(r'db\.netkeiba\.com/horse/\d+'))
            horse_id = None
            if a_horse:
                m_hid = re.search(r'/horse/(\d+)', a_horse['href'])
                horse_id = m_hid.group(1) if m_hid else None

            entries.append({
                'waku': waku,
                'umaban': umaban,
                'horse_name': horse_name,
                'horse_id': horse_id,
                'sex': sex,
                'age': age,
                'weight_carried': weight_carried,
                'jockey': jockey,
                'stable': trainer_raw,
                'trainer': trainer,
                'body_weight': body_weight,
                'body_weight_diff': bw_diff,
                'odds': odds,
            })
        except Exception as e:
            pass

    return {
        'race_id': race_id,
        'date': date_str,
        'venue_cd': venue_cd,
        'venue_name': venue_name,
        'race_num': race_num,
        'race_name': race_name,
        'class_code': class_code,
        'distance': distance,
        'track_type': track_type,
        'direction': direction,
        'weather': weather_m.group(1) if weather_m else None,
        'condition': cond_m.group(1) if cond_m else None,
        'start_time': start_time_m.group(1) if start_time_m else None,
        'heads_count': int(heads_m.group(1)) if heads_m else len(entries),
        'entries': entries,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', help='YYYY-MM-DD')
    parser.add_argument('--days', type=int, default=1)
    parser.add_argument('--output', default=str(OUT_PATH))
    args = parser.parse_args()

    start = date.fromisoformat(args.date) if args.date else date.today()
    dates = [(start + timedelta(days=i)).strftime('%Y%m%d') for i in range(args.days)]

    all_races = []
    for d_str in dates:
        race_ids = get_race_ids_for_date(d_str)
        if not race_ids:
            print(f"{d_str}: 南関東レースなし")
            time.sleep(SLEEP)
            continue
        print(f"{d_str}: {len(race_ids)}R取得中...")
        for rid in race_ids:
            soup = fetch(f"https://nar.netkeiba.com/race/shutuba.html?race_id={rid}")
            if soup:
                race = parse_shutsuba(soup, rid)
                all_races.append(race)
                print(f"  ✓ {rid} {race['venue_name']}{race['race_num']}R "
                      f"{race['class_code']} {race['distance']}m {race['heads_count']}頭")
            time.sleep(SLEEP)

    out = args.output
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(all_races, f, ensure_ascii=False, indent=2)
    print(f"\n保存: {out} ({len(all_races)}R)")
    return all_races


if __name__ == '__main__':
    main()
