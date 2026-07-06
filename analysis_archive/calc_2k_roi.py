"""新買い目ルール(1R=2,000円)でROI算出"""
import sqlite3, json
from collections import defaultdict

conn = sqlite3.connect('keiba.db')
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA cache_size=-65536")
conn.execute("PRAGMA temp_store=MEMORY")
conn.execute("PRAGMA mmap_size=268435456")

def is_weak_segment(grade, gap, odds):
    if grade == '1勝' and gap < 12: return True
    if grade == '3勝' and 8 <= gap < 12 and odds < 10: return True
    return False

# dividendsキャッシュ
div_cache = {}
for r in conn.execute('SELECT * FROM dividends WHERE date >= "2020-01-01"').fetchall():
    div_cache[(r['date'], r['venue'], r['race_num'])] = dict(r)

results_all = []

for year in range(2020, 2027):
    d = json.load(open(f'btv6_{year}.json', encoding='utf-8'))
    for b in d['bet_records']:
        is_sp = b.get('special', False)
        grade = b.get('grade', '')
        date = b['date']; venue = b['venue']; rn = b['race_num']
        div = div_cache.get((date, venue, rn))

        if is_sp:
            b['_year'] = year
            b['_type'] = 'C2'
            results_all.append(b)
            continue

        gap = b.get('gap', 0)
        odds = b.get('honmei_odds') or 0
        honmei_finish = b.get('honmei_finish', 99)
        ni_finish = b.get('ni_finish', 99)

        if not div:
            b['_year'] = year
            b['_type'] = 'v6weak' if is_weak_segment(grade, gap, odds) else 'v6normal'
            b['ret2k'] = 0
            results_all.append(b)
            continue

        tansho_ret = div['tansho_payout'] * 10 if honmei_finish == 1 else 0
        umaren_ret = div['umaren_payout'] * 10 if (honmei_finish <= 2 and ni_finish <= 2) else 0

        wide_ret = 0
        if honmei_finish <= 3 and ni_finish <= 3:
            ws = [div.get(f'wide{i}_payout', 0) or 0 for i in range(1, 4)]
            ws = [w for w in ws if w > 0]
            if ws:
                wide_ret = min(ws) * 10

        weak = is_weak_segment(grade, gap, odds)
        if weak:
            ret2k = tansho_ret + wide_ret
            b['_type'] = 'v6weak'
        else:
            ret2k = tansho_ret + umaren_ret
            b['_type'] = 'v6normal'

        b['_year'] = year
        b['ret2k'] = ret2k
        results_all.append(b)

# C2: 1R1頭制限(lap1最速) + ワイド x 1-3番人気
c2_bets = [r for r in results_all if r['_type'] == 'C2']
non_c2 = [r for r in results_all if r['_type'] != 'C2']

c2_by_race = defaultdict(list)
for b in c2_bets:
    c2_by_race[(b['date'], b['venue'], b['race_num'])].append(b)

c2_final = []
for key, bets in c2_by_race.items():
    date, venue, rn = key
    best = None; best_lap1 = 99
    for b in bets:
        row = conn.execute(
            "SELECT lap1 FROM training WHERE horse_name=? AND date BETWEEN date(?,'-14 days') AND date(?,'-1 day') AND lap1 IS NOT NULL AND lap1>0 ORDER BY lap1 LIMIT 1",
            (b['honmei_name'], date, date)).fetchone()
        lap1 = row['lap1'] if row else 99
        if lap1 < best_lap1:
            best_lap1 = lap1; best = b
    if best:
        c2_final.append(best)

for b in c2_final:
    date, venue, rn = b['date'], b['venue'], b['race_num']
    div = div_cache.get((date, venue, rn))
    finish = b.get('honmei_finish', 99)

    tansho_ret = div['tansho_payout'] * 11 if (finish == 1 and div) else 0

    wide_ret = 0
    if finish <= 3 and div:
        partners = conn.execute(
            'SELECT popularity, finish FROM results WHERE date=? AND venue=? AND race_num=? AND popularity IN (1,2,3) AND finish<90',
            (date, venue, rn)).fetchall()
        for p in partners:
            if p['finish'] <= 3:
                ws = [div.get(f'wide{i}_payout', 0) or 0 for i in range(1, 4)]
                ws = [w for w in ws if w > 0]
                if ws:
                    wide_ret += min(ws) * 3

    b['ret2k'] = tansho_ret + wide_ret

all_results = non_c2 + c2_final

# ── 出力 ──
print('=' * 105)
print('新買い目ルール(1R=2,000円) 全年バックテスト結果')
print('  v6通常: 単勝1,000+馬連1,000 | v6弱セグ: 単勝1,000+ワイド1,000 | C2: 単勝1,100+W300x3点')
print('=' * 105)

print()
print('[年別 x タイプ別]')
print(f"{'年':>6} | {'v6通常':>5} {'ROI':>7} {'損益':>10} | {'v6弱':>4} {'ROI':>7} {'損益':>9} | {'C2':>3} {'ROI':>7} {'損益':>9} | {'全体':>4} {'ROI':>7} {'損益':>10}")
print('-' * 105)

gt = defaultdict(lambda: [0, 0])
for year in range(2020, 2027):
    yr = [r for r in all_results if r['_year'] == year]
    parts = []
    for tp in ['v6normal', 'v6weak', 'C2']:
        sub = [r for r in yr if r['_type'] == tp]
        n = len(sub); inv = n*2000; ret = sum(r.get('ret2k', 0) for r in sub)
        gt[tp][0] += inv; gt[tp][1] += ret
        roi = ret/inv*100 if inv else 0
        parts.append((n, roi, ret - inv))
    n_all = len(yr); inv_all = n_all*2000; ret_all = sum(r.get('ret2k', 0) for r in yr)
    gt['all'][0] += inv_all; gt['all'][1] += ret_all
    roi_all = ret_all/inv_all*100 if inv_all else 0
    print(f"{year:>6} | {parts[0][0]:>4}R {parts[0][1]:>6.1f}% {parts[0][2]:>+10,} | {parts[1][0]:>3}R {parts[1][1]:>6.1f}% {parts[1][2]:>+9,} | {parts[2][0]:>3}R {parts[2][1]:>6.1f}% {parts[2][2]:>+9,} | {n_all:>4}R {roi_all:>6.1f}% {ret_all-inv_all:>+10,}")

print('-' * 105)
parts = []
for tp in ['v6normal', 'v6weak', 'C2']:
    inv, ret = gt[tp]; n = inv//2000; roi = ret/inv*100 if inv else 0
    parts.append((n, roi, ret - inv))
inv_a, ret_a = gt['all']; n_a = inv_a//2000; roi_a = ret_a/inv_a*100
print(f"{'合計':>6} | {parts[0][0]:>4}R {parts[0][1]:>6.1f}% {parts[0][2]:>+10,} | {parts[1][0]:>3}R {parts[1][1]:>6.1f}% {parts[1][2]:>+9,} | {parts[2][0]:>3}R {parts[2][1]:>6.1f}% {parts[2][2]:>+9,} | {n_a:>4}R {roi_a:>6.1f}% {ret_a-inv_a:>+10,}")

# クラス別
print()
print('[クラス別（全年合計）]')
print(f"{'クラス':>6} | {'件数':>5} {'投資':>10} {'回収':>10} {'ROI':>7} {'損益':>10}")
print('-' * 62)
gs = defaultdict(lambda: [0, 0, 0])
for r in all_results:
    gr = r.get('grade', '?')
    gs[gr][0] += 1; gs[gr][1] += 2000; gs[gr][2] += r.get('ret2k', 0)
for gr in ['新馬', '1勝', '2勝', '3勝', 'G3']:
    if gr not in gs: continue
    n, inv, ret = gs[gr]
    print(f"{gr:>6} | {n:>5}R {inv:>10,} {ret:>10,.0f} {ret/inv*100:>6.1f}% {ret-inv:>+10,.0f}")
n_t = sum(v[0] for v in gs.values()); inv_t = sum(v[1] for v in gs.values()); ret_t = sum(v[2] for v in gs.values())
print(f"{'全体':>6} | {n_t:>5}R {inv_t:>10,} {ret_t:>10,.0f} {ret_t/inv_t*100:>6.1f}% {ret_t-inv_t:>+10,.0f}")

# 年別×クラス別
print()
print('[年別 x クラス別 ROI]')
grades = ['1勝', '2勝', '3勝', 'G3', '新馬']
print(f"{'年':>6}", end='')
for g in grades: print(f" | {g:>6}", end='')
print(' |   全体')
print('-' * 65)
for year in range(2020, 2027):
    yr = [r for r in all_results if r['_year'] == year]
    print(f"{year:>6}", end='')
    for gr in grades:
        sub = [r for r in yr if r.get('grade') == gr]
        n = len(sub); inv = n*2000; ret = sum(r.get('ret2k', 0) for r in sub)
        if n == 0:
            print(f" |     --", end='')
        else:
            print(f" | {ret/inv*100:>5.0f}%", end='')
    n = len(yr); inv = n*2000; ret = sum(r.get('ret2k', 0) for r in yr)
    print(f" | {ret/inv*100:>5.0f}%")
