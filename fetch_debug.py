"""大阪杯1レースだけ取得してHTML構造をデバッグ"""
import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

options = Options()
options.add_argument('--headless=new')
options.add_argument('--no-sandbox')
options.add_argument('--disable-gpu')
options.add_argument('--window-size=1920,1080')

service = Service(ChromeDriverManager().install())
driver = webdriver.Chrome(service=service, options=options)

url = "https://race.netkeiba.com/race/shutuba.html?race_id=202609020411"
print(f"Fetching: {url}")
driver.get(url)
time.sleep(5)

# ページのエンコーディング確認
print(f"Title: {driver.title}")
print(f"Encoding meta: {driver.execute_script('return document.characterSet')}")

# レース名
try:
    el = driver.find_element(By.CSS_SELECTOR, '.RaceName')
    print(f"RaceName: [{el.text}]")
except Exception as e:
    print(f"RaceName error: {e}")

# 出走馬テーブルの構造を詳しく見る
rows = driver.find_elements(By.CSS_SELECTOR, 'table.Shutuba_Table tr.HorseList')
print(f"\nHorseList rows: {len(rows)}")

if rows:
    # 1行目のHTML構造を出力
    first_row = rows[0]
    tds = first_row.find_elements(By.TAG_NAME, 'td')
    print(f"TDs in first row: {len(tds)}")
    for i, td in enumerate(tds):
        cls = td.get_attribute('class') or ''
        txt = td.text.strip()[:30]
        print(f"  td[{i}] class={cls:20s} text=[{txt}]")

    # 全馬の情報を取得
    print("\n=== All horses ===")
    for row in rows:
        tds = row.find_elements(By.TAG_NAME, 'td')
        if len(tds) < 4: continue
        # 各セルの中身を見る
        waku = tds[0].text.strip() if len(tds) > 0 else ''
        umaban = tds[1].text.strip() if len(tds) > 1 else ''
        # 馬名はリンクテキスト
        try:
            name_el = row.find_element(By.CSS_SELECTOR, 'span.HorseName a')
            name = name_el.text.strip()
        except:
            name = tds[3].text.strip()[:15] if len(tds) > 3 else ''
        # 騎手
        try:
            jockey_el = row.find_element(By.CSS_SELECTOR, 'td.Jockey a')
            jockey = jockey_el.text.strip()
        except:
            jockey = ''
        print(f"  waku={waku:>2} umaban={umaban:>2} name={name:>15} jockey={jockey}")

driver.quit()
