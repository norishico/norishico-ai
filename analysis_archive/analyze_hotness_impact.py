"""
血統の旬 (Hotness) - 既存BTデータでの効果検証

各bet_recordのhonmei馬の父の「直近30日vs過去1年」乖離を計算し、
hot/cold 別にROI を集計。有意な差があれば実装価値あり。
"""
import json, sqlite3, sys, io
from collections import defaultdict
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

conn = sqlite3.connect('keiba.db')
conn.row_factory = sqlite3.Row

YEARS = [2020,2021,2022,2023,2024,2025,2026]

# キャッシュ: (sire, date) → hotness
_hot_cache = {}

def sire_hotness(sire, race_date):
    """直近30日 vs 過去1年(31-365日前)の複勝率乖離"""
    key = (sire, race_date)
    if key in _hot_cache:
        return _hot_cache[key]
    # 直近30日
    r1 = conn.execute('''
        SELECT COUNT(*) as n, SUM(CASE WHEN finish<=3 THEN 1.0 ELSE 0 END) as t3
        FROM results WHERE sire=? AND date BETWEEN date(?, '-30 days') AND date(?, '-1 days') AND finish<90
    ''', (sire, race_date, race_date)).fetchone()
    # 過去1年 (31-365日前)
    r2 = conn.execute('''
        SELECT COUNT(*) as n, SUM(CASE WHEN finish<=3 THEN 1.0 ELSE 0 END) as t3
        FROM results WHERE sire=? AND date BETWEEN date(?, '-365 days') AND date(?, '-31 days') AND finish<90
    ''', (sire, race_date, race_date)).fetchone()
    # 十分なサンプル必要
    if r1['n'] < 20 or r2['n'] < 100:
        _hot_cache[key] = 0
        return 0
    recent_rate = r1['t3'] / r1['n'] * 100
    baseline_rate = r2['t3'] / r2['n'] * 100
    dev = recent_rate - baseline_rate
    # ±5pt に抑制
    dev = max(-5, min(5, dev))
    _hot_cache[key] = dev
    return dev


def get_honmei_sire(date, venue, race_num, horse_name):
    """bet_recordのhonmeiに対する父名取得"""
    r = conn.execute('''
        SELECT sire FROM results
        WHERE date=? AND venue=? AND race_num=? AND horse_name=?
    ''', (date, venue, race_num, horse_name)).fetchone()
    return r['sire'] if r and r['sire'] else None


def main():
    print('='*80)
    print('【血統の旬 Hotness 効果検証】')
    print('='*80)

    # 全bet_records集計 (hotness別)
    buckets = defaultdict(lambda: {'n':0,'wins':0,'cost':0,'ret':0})

    print('bet_records 読込中...')
    total_bets = 0
    for y in YEARS:
        d = json.load(open(f'btv6_{y}.json', encoding='utf-8'))
        for b in d['bet_records']:
            total_bets += 1
            honmei = b.get('honmei_name', '')
            if not honmei: continue
            sire = get_honmei_sire(b['date'], b['venue'], b['race_num'], honmei)
            if not sire: continue
            hot = sire_hotness(sire, b['date'])
            # bucket key
            if hot >= 3: bk = '🟢 +3pt以上 (好調)'
            elif hot >= 1: bk = '🟢 +1〜3pt'
            elif hot > -1: bk = '⚪ 中立(-1〜+1)'
            elif hot > -3: bk = '🔴 -1〜-3pt'
            else: bk = '🔴 -3pt以下 (不調)'
            buckets[bk]['n'] += 1
            buckets[bk]['cost'] += b.get('cost', 0)
            buckets[bk]['ret'] += b.get('ret', 0)
            if b.get('honmei_finish') == 1: buckets[bk]['wins'] += 1

    print(f'  全{total_bets}bets 処理完了')
    print()

    # 結果表示
    print('='*80)
    print('【hotness別の bet結果】')
    print('='*80)
    print(f'{"hotness":>24} | {"件数":>6} {"勝":>5} {"勝率":>6} {"ROI":>7} {"損益":>12}')
    print('-'*80)
    order = ['🟢 +3pt以上 (好調)', '🟢 +1〜3pt', '⚪ 中立(-1〜+1)', '🔴 -1〜-3pt', '🔴 -3pt以下 (不調)']
    for bk in order:
        v = buckets.get(bk)
        if not v or v['n'] == 0: continue
        wr = v['wins']/v['n']*100
        roi = v['ret']/v['cost']*100 if v['cost'] else 0
        prof = v['ret']-v['cost']
        print(f'{bk:>24} | {v["n"]:>6} {v["wins"]:>5} {wr:>5.1f}% {roi:>6.1f}% {prof:>+12,}')

    # hotness の絶対値で ROI の差を見る
    print()
    print('【単純化: hot(+1pt以上) vs cold(-1pt以下) vs neutral】')
    h = defaultdict(lambda: {'n':0,'wins':0,'cost':0,'ret':0})
    for y in YEARS:
        d = json.load(open(f'btv6_{y}.json', encoding='utf-8'))
        for b in d['bet_records']:
            honmei = b.get('honmei_name', '')
            if not honmei: continue
            sire = get_honmei_sire(b['date'], b['venue'], b['race_num'], honmei)
            if not sire: continue
            hot = sire_hotness(sire, b['date'])
            cat = 'hot' if hot >= 1 else ('cold' if hot <= -1 else 'neutral')
            h[cat]['n'] += 1
            h[cat]['cost'] += b.get('cost', 0)
            h[cat]['ret'] += b.get('ret', 0)
            if b.get('honmei_finish') == 1: h[cat]['wins'] += 1

    for cat in ['hot','neutral','cold']:
        v = h[cat]
        if v['n'] == 0: continue
        roi = v['ret']/v['cost']*100 if v['cost'] else 0
        prof = v['ret']-v['cost']
        wr = v['wins']/v['n']*100
        print(f'  {cat:>8}: {v["n"]:>4}R 勝率{wr:>4.1f}% ROI{roi:>6.1f}% 損益{prof:>+10,}')

    # cold 除外 シミュレーション
    print()
    print('【cold馬を除外した場合】')
    total = sum(v['cost'] for v in h.values())
    total_ret = sum(v['ret'] for v in h.values())
    total_prof = total_ret - total
    total_roi = total_ret/total*100
    print(f'  全体: {sum(v["n"] for v in h.values())}R ROI{total_roi:.1f}% 損益{total_prof:+,}')
    excluded_cost = total - h['cold']['cost']
    excluded_ret = total_ret - h['cold']['ret']
    excluded_n = sum(v['n'] for k,v in h.items() if k != 'cold')
    excluded_roi = excluded_ret/excluded_cost*100 if excluded_cost else 0
    print(f'  cold除外: {excluded_n}R ROI{excluded_roi:.1f}% 損益{excluded_ret-excluded_cost:+,}')
    improvement = (excluded_ret - excluded_cost) - total_prof
    print(f'  改善: {improvement:+,}円')


if __name__ == '__main__':
    main()
