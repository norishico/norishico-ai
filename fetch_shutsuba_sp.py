"""sp.netkeiba.com フォールバックスクレイパー
race.netkeiba.com が HTTP 400 / IPブロック時に使用する。
Selenium でページを開いてオッズを待ち、page_source を BeautifulSoup でパースする。
"""
import time, json, re, sys
import requests
from bs4 import BeautifulSoup
from pathlib import Path
from fetch_shutsuba import create_driver

SP_BASE = "https://race.sp.netkeiba.com/"
SP_TOP  = "https://sp.netkeiba.com/"
MOBILE_UA = {'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0) AppleWebKit/605.1.15'}
PROJ = Path(__file__).parent

_VENUE_MAP = {'01':'札幌','02':'函館','03':'福島','04':'新潟','05':'東京',
              '06':'中山','07':'中京','08':'京都','09':'阪神','10':'小倉'}


def get_kaisai_ids_for_weekend(driver):
    """sp.netkeiba.comトップから今週末の kaisai_id を取得"""
    driver.get(SP_TOP)
    time.sleep(4)
    ids = re.findall(r'kaisai_id=(\d{10})', driver.page_source)
    unique = list(dict.fromkeys(ids))
    # 今年のものだけ
    from datetime import date
    this_year = str(date.today().year)
    filtered = [k for k in unique if k.startswith(this_year)]
    print(f"kaisai_ids (this year): {filtered}")
    return filtered


def get_race_ids_for_kaisai(kaisai_id):
    """kaisai_id に属する race_id リストを requests で取得"""
    url = f"{SP_BASE}?pid=race_list&kaisai_id={kaisai_id}"
    r = requests.get(url, headers=MOBILE_UA, timeout=15)
    if r.status_code != 200:
        return []
    html = r.content.decode('euc-jp', errors='replace')
    ids = re.findall(r'race_id=(\d{12})', html)
    return list(dict.fromkeys(ids))


def fetch_shutsuba_sp(driver, race_id):
    """Selenium で shutsuba ページを開き、BS4 でパースして race dict を返す"""
    url = f"{SP_BASE}?pid=shutuba&race_id={race_id}"
    driver.get(url)
    time.sleep(4)  # JS odds 読み込み待ち

    src = driver.page_source
    soup = BeautifulSoup(src, 'html.parser')

    race_info = {
        'race_id': race_id,
        'date': '', 'race_name': '', 'grade': '',
        'venue': '', 'race_num': int(race_id[-2:]),
        'distance': 0, 'surface': '芝',
        'track_cond': '良', 'start_time': '',
        'horses': [], 'race_detail': '', 'race_data2': '',
    }

    # --- タイトルからメタデータ抽出 ---
    title = driver.title or soup.title.text if soup.title else ''
    # 例: "青葉賞(G2) 出馬表 | 2026年4月25日 東京11R レース情報(JRA) - netkeiba"
    m_date = re.search(r'(\d{4})年(\d+)月(\d+)日', title)
    if m_date:
        race_info['date'] = f"{m_date.group(1)}-{int(m_date.group(2)):02d}-{int(m_date.group(3)):02d}"
    m_race = re.search(r'(\S+?)(\d+)R', title)
    if m_race:
        race_info['venue'] = m_race.group(1).strip()
        race_info['race_num'] = int(m_race.group(2))
    m_name = re.match(r'^(.+?)(?:\(|\s+出馬表|\s+\|)', title)
    if m_name:
        name_raw = m_name.group(1).strip()
        # グレードを除去
        race_info['race_name'] = re.sub(r'\((G[123]|L|OP)\)', '', name_raw).strip()
    m_grade = re.search(r'\((G[123]|L|OP)\)', title)
    if m_grade:
        race_info['grade'] = m_grade.group(1)

    # --- 距離・馬場・発走時刻 (SP版: .RaceData01 不在のため page_source 全体から抽出) ---
    # SP page 例: "ダート1600m(右) 13頭" / "芝外2400m" / コース情報h2 など
    m_dist = re.search(r'(ダート|芝[外]?|障[害外]?)\s*(\d{3,4})m', src)
    if m_dist:
        surf_txt = m_dist.group(1)
        race_info['surface'] = 'ダ' if surf_txt.startswith('ダ') else ('障' if surf_txt.startswith('障') else '芝')
        race_info['distance'] = int(m_dist.group(2))
    if not race_info['distance']:
        # 分離span構造フォールバック: <span>障</span><span>3170m</span>
        for span in soup.find_all('span'):
            m_d2 = re.fullmatch(r'(\d{3,4})m', span.get_text(strip=True))
            if m_d2:
                race_info['distance'] = int(m_d2.group(1))
                prev = span.find_previous_sibling('span')
                if prev:
                    pt = prev.get_text(strip=True)
                    if pt in ('ダ', 'ダート'):
                        race_info['surface'] = 'ダ'
                    elif pt in ('障', '障害'):
                        race_info['surface'] = '障'
                break
    m_cond = re.search(r'馬場:(\S+)', src)
    if m_cond:
        race_info['track_cond'] = m_cond.group(1)
    # 発走時刻: div.Race_Data の先頭に "16:30 <span..." の形式
    race_data_div = soup.select_one('.Race_Data')
    if race_data_div:
        m_time = re.match(r'\s*(\d{1,2}:\d{2})', race_data_div.get_text(' ', strip=True))
        if m_time:
            race_info['start_time'] = m_time.group(1)
    if not race_info['start_time']:
        m_time = re.search(r'発走[^\d]*(\d{1,2}:\d{2})|(\d{1,2}:\d{2})[^\d]*発走', src)
        if m_time:
            race_info['start_time'] = m_time.group(1) or m_time.group(2)

    # --- 距離フォールバック: race_id から venue コード経由 ---
    if not race_info['venue']:
        vc = race_id[4:6]
        race_info['venue'] = _VENUE_MAP.get(vc, '')

    # --- 出走馬パース ---
    for row in soup.select('tr.HorseList'):
        h = {'waku': 0, 'umaban': 0, 'name': '', 'age': '',
             'weight': '', 'jockey': '', 'trainer': '', 'odds': None, 'popularity': None}

        # 枠番: class="WakuN" の N
        waku_td = row.select_one('td[class^="Waku"]')
        if waku_td:
            m_w = re.search(r'Waku(\d)', waku_td.get('class', [''])[0])
            h['waku'] = int(m_w.group(1)) if m_w else 0

        # 馬番: input value="UMABAN_XX"
        inp = row.select_one('td.Horse_Select input')
        if inp:
            val = inp.get('value', '')
            try:
                h['umaban'] = int(val.split('_')[0])
            except Exception:
                pass

        # 馬名
        horse_a = row.select_one('dt.Horse a, .Horse a')
        if horse_a:
            h['name'] = horse_a.get_text(strip=True)

        # 馬齢 (dd.Age の直接テキストのみ — 子要素リンクを除外)
        age_dd = row.select_one('dd.Age')
        if age_dd:
            age_raw = age_dd.get_text(strip=True)
            m_age = re.match(r'([牡牝セ騸]\d+)', age_raw)
            h['age'] = m_age.group(1) if m_age else age_raw[:3]

        # 騎手・斤量
        jockey_a = row.select_one('dd.Jockey a')
        if jockey_a:
            jockey_em = jockey_a.find('em')
            if jockey_em:
                h['jockey'] = jockey_em.get_text(strip=True)
            full_text = jockey_a.get_text(strip=True)
            weight_part = full_text.replace(h['jockey'], '').strip()
            m_kg = re.search(r'(\d+(?:\.\d+)?)', weight_part)
            if m_kg:
                h['weight'] = m_kg.group(1)

        # オッズ (JS で読み込まれた値)
        odds_span = row.select_one("span[id^='odds-']")
        if odds_span:
            odds_text = odds_span.get_text(strip=True)
            try:
                h['odds'] = float(odds_text)
            except Exception:
                h['odds'] = None

        # 人気
        ninki_span = row.select_one("span[id^='ninki-']")
        if ninki_span:
            try:
                h['popularity'] = int(ninki_span.get_text(strip=True))
            except Exception:
                pass

        # 取消・除外馬の検出(2026-09-20 F1追加、fetch_shutsuba.pyと同一方針)
        row_classes = row.get('class', []) or []
        row_text = row.get_text(' ', strip=True)
        h['scratched'] = bool(
            any('Torikeshi' in c or 'Cancel' in c for c in row_classes)
            or '取消' in row_text or '除外' in row_text
        )

        if h['name']:
            race_info['horses'].append(h)

    n = len(race_info['horses'])
    d = race_info['distance']
    s = race_info['surface']
    c = race_info['track_cond']
    race_info['race_detail'] = f"{s}{d}m {n}頭 {c}" if d else ''
    g = race_info['grade']
    race_info['race_data2'] = f"{g} " if g else ''

    return race_info


def fetch_this_week_races_sp(driver):
    """今週末の全レースを取得して this_week_races.json 互換リストを返す"""
    kaisai_ids = get_kaisai_ids_for_weekend(driver)
    if not kaisai_ids:
        print("No kaisai_ids found — using fallback from JV-Link RA")
        return []

    all_race_ids = []
    for kid in kaisai_ids:
        rids = get_race_ids_for_kaisai(kid)
        print(f"  kaisai {kid}: {len(rids)} races")
        all_race_ids.extend(rids)

    # 重複除去
    all_race_ids = list(dict.fromkeys(all_race_ids))
    print(f"Total races to fetch: {len(all_race_ids)}")

    races = []
    for i, rid in enumerate(all_race_ids, 1):
        try:
            r = fetch_shutsuba_sp(driver, rid)
            if r and r.get('horses'):
                if r.get('surface') == '障':
                    print(f"  [{i}/{len(all_race_ids)}] SKIP(障害) {r.get('venue','?')}{r.get('race_num',0)}R {r.get('race_name','')[:12]}")
                else:
                    races.append(r)
                    odds0 = r['horses'][0].get('odds') if r['horses'] else None
                    print(f"  [{i}/{len(all_race_ids)}] {r.get('venue','?')}{r.get('race_num',0)}R "
                          f"{r.get('race_name','')[:12]} {len(r['horses'])}頭 odds[0]={odds0}")
            time.sleep(0.3)
        except Exception as e:
            print(f"  [{i}] SKIP {rid}: {e}")

    return races


def main():
    driver = create_driver()
    try:
        races = fetch_this_week_races_sp(driver)
    finally:
        driver.quit()

    if not races:
        print("No races fetched.")
        sys.exit(1)

    out = PROJ / 'this_week_races.json'
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(races, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(races)} races → {out}")


if __name__ == '__main__':
    main()
