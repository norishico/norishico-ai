"""大阪杯+ダービー卿CTの2レースだけ取得して確認"""
import json
from fetch_shutsuba import create_driver, fetch_shutsuba
import time

driver = create_driver()
races = []

for rid in ['202609020411', '202606030311']:  # 大阪杯, ダービー卿CT
    r = fetch_shutsuba(driver, rid)
    races.append(r)
    time.sleep(3)

driver.quit()

with open('sample_races.json', 'w', encoding='utf-8') as f:
    json.dump(races, f, ensure_ascii=False, indent=2)

for r in races:
    print(f"\n{r['venue']}{r['race_num']}R {r['race_name']} {r.get('start_time','')} {r.get('surface','')}{r.get('distance','')}m")
    for h in r['horses']:
        print(f"  {h.get('waku',0):>1}枠{h.get('umaban',0):>2}番 {h['name']:>15} {h['jockey']:>6} odds={h.get('odds','--'):>5} {h.get('popularity',''):>2}人気")
