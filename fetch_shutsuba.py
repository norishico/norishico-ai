"""netkeibaから出走表+想定オッズを取得するスクリプト
Selenium使用、サイトに負荷をかけないよう十分なwaitを入れる
"""
import time
import json
import re
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


def create_driver():
    """ヘッドレスChromeドライバーを作成"""
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--lang=ja')
    options.add_argument('--accept-charset=utf-8')
    options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
    options.add_experimental_option('prefs', {'intl.accept_languages': 'ja,en'})

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def fetch_race_list(driver, date_str):
    """指定日のレース一覧を取得
    date_str: 'YYYYMMDD' 形式
    """
    url = f"https://race.netkeiba.com/top/race_list.html?kaisai_date={date_str}"
    print(f"Fetching race list: {date_str}")
    driver.get(url)
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="race_id="]'))
        )
    except Exception:
        pass

    races = []
    try:
        # レースリンクを取得
        race_links = driver.find_elements(By.CSS_SELECTOR, 'a[href*="race_id="]')
        seen = set()
        for link in race_links:
            href = link.get_attribute('href') or ''
            m = re.search(r'race_id=(\d+)', href)
            if m and m.group(1) not in seen:
                race_id = m.group(1)
                seen.add(race_id)
                text = link.text.strip()
                races.append({'race_id': race_id, 'text': text})
    except Exception as e:
        print(f"  Error: {e}")

    print(f"  Found {len(races)} races")
    return races


def fetch_shutsuba(driver, race_id):
    """1レースの出走表を取得"""
    url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
    print(f"  Fetching shutsuba: {race_id}")
    driver.get(url)
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'table.Shutuba_Table tr.HorseList'))
        )
    except Exception:
        pass

    race_info = {
        'race_id': race_id,
        'race_name': '',
        'venue': '',
        'race_num': 0,
        'distance': '',
        'surface': '',
        'track_cond': '',
        'start_time': '',
        'horses': [],
    }

    try:
        # レース名
        try:
            name_el = driver.find_element(By.CSS_SELECTOR, '.RaceName')
            race_info['race_name'] = name_el.text.strip()
        except:
            pass

        # レース詳細（距離、馬場等）
        try:
            data_el = driver.find_element(By.CSS_SELECTOR, '.RaceData01')
            data_text = data_el.text.strip()
            race_info['race_detail'] = data_text
            # 発走時刻
            m = re.search(r'(\d{1,2}:\d{2})', data_text)
            if m: race_info['start_time'] = m.group(1)
            # 距離
            m = re.search(r'(芝|ダ|ダート)\s*(\d+)m', data_text)
            if m:
                race_info['surface'] = '芝' if m.group(1) == '芝' else 'ダ'
                race_info['distance'] = int(m.group(2))
            # 馬場状態
            m = re.search(r'馬場[：:]?\s*(良|稍重?|重|不良)', data_text)
            if m:
                c = m.group(1)
                race_info['track_cond'] = '稍重' if c == '稍' else c
        except:
            pass

        # レース番号・会場
        try:
            data2_el = driver.find_element(By.CSS_SELECTOR, '.RaceData02')
            data2_text = data2_el.text.strip()
            race_info['race_data2'] = data2_text
            # 会場
            for v in ['札幌','函館','福島','新潟','東京','中山','中京','京都','阪神','小倉']:
                if v in data2_text:
                    race_info['venue'] = v
                    break
            # R番号
            m = re.search(r'(\d+)R', data2_text)
            if m: race_info['race_num'] = int(m.group(1))
        except:
            pass

        # race_numのフォールバック: race_idから（末尾2桁がR番号）
        if race_info['race_num'] == 0:
            try:
                race_info['race_num'] = int(race_id[-2:])
            except:
                pass

        # 出走馬テーブル
        rows = driver.find_elements(By.CSS_SELECTOR, 'table.Shutuba_Table tr.HorseList')
        for row in rows:
            horse = {}
            try:
                tds = row.find_elements(By.TAG_NAME, 'td')
                if len(tds) < 10: continue

                # 枠番・馬番（確定前は空）
                horse['waku'] = int(tds[0].text.strip()) if tds[0].text.strip().isdigit() else 0
                horse['umaban'] = int(tds[1].text.strip()) if tds[1].text.strip().isdigit() else 0

                # 馬名
                try:
                    name_el = row.find_element(By.CSS_SELECTOR, 'span.HorseName a')
                    horse['name'] = name_el.text.strip()
                except:
                    horse['name'] = tds[3].text.strip()[:20]

                # 馬齢
                horse['age'] = tds[4].text.strip() if len(tds) > 4 else ''

                # 斤量
                horse['weight'] = tds[5].text.strip() if len(tds) > 5 else ''

                # 騎手
                try:
                    jockey_el = row.find_element(By.CSS_SELECTOR, 'td.Jockey a')
                    horse['jockey'] = jockey_el.text.strip()
                except:
                    horse['jockey'] = tds[6].text.strip() if len(tds) > 6 else ''

                # 調教師
                horse['trainer'] = tds[7].text.strip() if len(tds) > 7 else ''

                # 想定オッズ（td.Popular）
                # netkeibaは切替時間帯に '**.**' や '.' をマスク表示 → 小数含むパターン必須で弾く
                try:
                    pop_td = row.find_element(By.CSS_SELECTOR, 'td.Popular')
                    odds_text = pop_td.text.strip()
                    import re as _re
                    odds_match = _re.search(r'\d+\.\d+', odds_text)
                    horse['odds'] = odds_match.group() if odds_match else ''
                except:
                    horse['odds'] = ''

                # 人気順位
                try:
                    ninki_el = row.find_element(By.CSS_SELECTOR, 'td.Popular_Ninki')
                    horse['popularity'] = ninki_el.text.strip()
                except:
                    horse['popularity'] = ''

                if horse['name']:
                    race_info['horses'].append(horse)

            except Exception as e:
                continue

    except Exception as e:
        print(f"    Error parsing: {e}")

    print(f"    {race_info['venue']}{race_info['race_num']}R {race_info['race_name']} "
          f"{len(race_info['horses'])}頭")
    return race_info


def main():
    print("Starting Selenium driver...")
    driver = create_driver()

    try:
        # 今週末の日付（2026年4月4日土曜、5日日曜）
        dates = ['20260404', '20260405']
        all_races = []

        for date_str in dates:
            races = fetch_race_list(driver, date_str)
            time.sleep(2)  # サイトに優しく

            for race in races:
                race_data = fetch_shutsuba(driver, race['race_id'])
                all_races.append(race_data)
                time.sleep(3)  # レース間の待ち（サイトに優しく）

        # JSON保存
        outfile = 'this_week_races.json'
        with open(outfile, 'w', encoding='utf-8') as f:
            json.dump(all_races, f, ensure_ascii=False, indent=2)

        print(f"\n=== 完了 ===")
        print(f"取得レース数: {len(all_races)}")
        for r in all_races:
            if r['horses']:
                print(f"  {r['venue']}{r['race_num']}R {r['race_name']} {len(r['horses'])}頭")
        print(f"→ {outfile} に保存")

    finally:
        driver.quit()


if __name__ == '__main__':
    main()
