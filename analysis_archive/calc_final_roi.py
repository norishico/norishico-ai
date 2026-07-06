"""v6.2+C2+F1 最終ROI算出（JSONから正確に集計）"""
import json
from collections import defaultdict

print("=" * 110)
print("v6.2 + C2 + F1  最終バックテスト結果")
print("  v6本体: 単勝1,000+馬連1,000=2,000円 | C2: 単勝1,100+W300x3=2,000円 | F1: 単勝1,000円")
print("=" * 110)
print()

# 年別×タイプ別
print("[年別 x タイプ別]")
hdr = f"{'年':>6} {'全R':>5} | {'v6':>4} {'inv':>8} {'ROI':>6} {'損益':>9} | {'C2':>4} {'inv':>7} {'ROI':>6} {'損益':>9} | {'F1':>3} {'inv':>6} {'ROI':>6} {'損益':>8} | {'全体':>4} {'inv':>8} {'ROI':>6} {'損益':>9}"
print(hdr)
print("-" * 120)

gt = defaultdict(lambda: [0, 0, 0])  # [n, inv, ret]
total_all_r = 0

for year in range(2020, 2027):
    d = json.load(open(f'btv6_{year}.json', encoding='utf-8'))
    all_r = len(d['all_races'])
    total_all_r += all_r
    bets = d['bet_records']

    v6 = [b for b in bets if not b.get('special')]
    c2 = [b for b in bets if b.get('rule') == 'C2_新馬accel']
    f1 = [b for b in bets if b.get('rule') == 'F1_未勝利主流accel']

    parts = []
    for tp, sub in [('v6', v6), ('C2', c2), ('F1', f1)]:
        n = len(sub)
        inv = sum(b.get('cost', 1000) for b in sub)
        ret = sum(b.get('ret', 0) for b in sub)
        roi = ret / inv * 100 if inv else 0
        pf = ret - inv
        gt[tp][0] += n; gt[tp][1] += inv; gt[tp][2] += ret
        parts.append((n, inv, roi, pf))

    # 全体
    n_all = len(bets)
    inv_all = sum(b.get('cost', 1000) for b in bets)
    ret_all = sum(b.get('ret', 0) for b in bets)
    roi_all = ret_all / inv_all * 100 if inv_all else 0
    pf_all = ret_all - inv_all
    gt['all'][0] += n_all; gt['all'][1] += inv_all; gt['all'][2] += ret_all

    p = parts
    print(f"{year:>6} {all_r:>5} | {p[0][0]:>4}R {p[0][1]:>8,} {p[0][2]:>5.1f}% {p[0][3]:>+9,} | {p[1][0]:>4}R {p[1][1]:>7,} {p[1][2]:>5.1f}% {p[1][3]:>+9,} | {p[2][0]:>3}R {p[2][1]:>6,} {p[2][2]:>5.1f}% {p[2][3]:>+8,} | {n_all:>4}R {inv_all:>8,} {roi_all:>5.1f}% {pf_all:>+9,}")

print("-" * 120)
# 合計
parts_t = []
for tp in ['v6', 'C2', 'F1']:
    n, inv, ret = gt[tp]
    roi = ret / inv * 100 if inv else 0
    pf = ret - inv
    parts_t.append((n, inv, roi, pf))
n_a, inv_a, ret_a = gt['all']
roi_a = ret_a / inv_a * 100 if inv_a else 0
pf_a = ret_a - inv_a
p = parts_t
print(f"{'合計':>6} {total_all_r:>5} | {p[0][0]:>4}R {p[0][1]:>8,} {p[0][2]:>5.1f}% {p[0][3]:>+9,} | {p[1][0]:>4}R {p[1][1]:>7,} {p[1][2]:>5.1f}% {p[1][3]:>+9,} | {p[2][0]:>3}R {p[2][1]:>6,} {p[2][2]:>5.1f}% {p[2][3]:>+8,} | {n_a:>4}R {inv_a:>8,} {roi_a:>5.1f}% {pf_a:>+9,}")

# クラス別
print()
print("[クラス別（全年合計）]")
print(f"  {'クラス':>6} {'件数':>5} {'投資':>10} {'回収':>10} {'ROI':>6} {'損益':>10}")
print("  " + "-" * 55)
gs = defaultdict(lambda: [0, 0, 0])
for year in range(2020, 2027):
    d = json.load(open(f'btv6_{year}.json', encoding='utf-8'))
    for b in d['bet_records']:
        gr = b.get('grade', '?')
        cost = b.get('cost', 1000)
        ret = b.get('ret', 0)
        gs[gr][0] += 1; gs[gr][1] += cost; gs[gr][2] += ret
for gr in ['新馬', '未勝利', '1勝', '2勝', '3勝', 'G3']:
    if gr not in gs: continue
    n, inv, ret = gs[gr]
    print(f"  {gr:>6} {n:>5}R {inv:>10,} {ret:>10,.0f} {ret/inv*100:>5.1f}% {ret-inv:>+10,.0f}")
n_t = sum(v[0] for v in gs.values())
inv_t = sum(v[1] for v in gs.values())
ret_t = sum(v[2] for v in gs.values())
print(f"  {'全体':>6} {n_t:>5}R {inv_t:>10,} {ret_t:>10,.0f} {ret_t/inv_t*100:>5.1f}% {ret_t-inv_t:>+10,.0f}")

# 年別×クラス別ROI
print()
print("[年別 x クラス別 ROI]")
grades = ['1勝', '2勝', '3勝', 'G3', '新馬', '未勝利']
hdr2 = f"  {'年':>6}"
for g in grades:
    hdr2 += f" {g:>6}"
hdr2 += "   全体"
print(hdr2)
print("  " + "-" * 60)
for year in range(2020, 2027):
    d = json.load(open(f'btv6_{year}.json', encoding='utf-8'))
    bets = d['bet_records']
    line = f"  {year:>6}"
    for gr in grades:
        sub = [b for b in bets if b.get('grade') == gr]
        n = len(sub)
        if n == 0:
            line += f" {'--':>6}"
        else:
            inv = sum(b.get('cost', 1000) for b in sub)
            ret = sum(b.get('ret', 0) for b in sub)
            roi = ret / inv * 100 if inv else 0
            line += f" {roi:>5.0f}%"
    # 全体
    inv = sum(b.get('cost', 1000) for b in bets)
    ret = sum(b.get('ret', 0) for b in bets)
    roi = ret / inv * 100 if inv else 0
    line += f"  {roi:>5.0f}%"
    print(line)
