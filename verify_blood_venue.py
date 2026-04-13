"""血統×会場パターンの信頼性検証
1. 年度安定性（全年/前半/後半で複勝率が安定しているか）
2. サンプル50件以上の堅いパターンの絞り込み
3. 既存score_bloodline(surface×距離帯)との差分
"""
import sqlite3
from collections import defaultdict

conn = sqlite3.connect('keiba.db')
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA cache_size=-65536")
conn.execute("PRAGMA temp_store=MEMORY")
conn.execute("PRAGMA mmap_size=268435456")

print("Loading...")
rows = conn.execute("""
    SELECT venue, distance, surface, track_cond, TRIM(sire) as sire,
           finish, odds, umaban, num_horses, date
    FROM results
    WHERE finish IS NOT NULL AND finish < 90 AND finish > 0
      AND odds IS NOT NULL AND odds > 0
      AND sire IS NOT NULL AND TRIM(sire) != ''
      AND umaban > 0 AND num_horses > 0
""").fetchall()
print(f"Loaded {len(rows)} rows")

# ── ① 429パターンの年度安定性 ──
print("\n" + "=" * 80)
print("検証1: 会場×距離×父 パターンの年度安定性")
print("=" * 80)

vds_yearly = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0.0]))
for r in rows:
    key = (r['venue'], r['distance'], r['sire'])
    yr = r['date'][:4]
    vds_yearly[key][yr][0] += 1
    if r['finish'] <= 3: vds_yearly[key][yr][1] += 1
    if r['finish'] == 1:
        vds_yearly[key][yr][2] += 1
        vds_yearly[key][yr][3] += r['odds']

# 全年合計でフィルタ
stable_patterns = []
all_qualifying = []

for key, yearly in vds_yearly.items():
    total_n = sum(v[0] for v in yearly.values())
    total_t3 = sum(v[1] for v in yearly.values())
    total_wins = sum(v[2] for v in yearly.values())
    total_odds = sum(v[3] for v in yearly.values())
    if total_n < 20: continue
    t3r = total_t3 / total_n * 100
    roi = total_odds / total_n * 100
    if not (t3r >= 80 or roi >= 150): continue

    all_qualifying.append(key)

    # 年度安定性: 3年以上データがあり、各年で複勝率30%以上の年が過半数
    years_with_data = [(yr, v) for yr, v in yearly.items() if v[0] >= 3]
    if len(years_with_data) < 3: continue

    years_above_30 = sum(1 for yr, v in years_with_data if v[1] / v[0] * 100 >= 30)
    stability = years_above_30 / len(years_with_data)

    # 前半(2019-2022) vs 後半(2023-2026)
    first_n = sum(v[0] for yr, v in yearly.items() if yr <= '2022')
    first_t3 = sum(v[1] for yr, v in yearly.items() if yr <= '2022')
    second_n = sum(v[0] for yr, v in yearly.items() if yr >= '2023')
    second_t3 = sum(v[1] for yr, v in yearly.items() if yr >= '2023')
    first_rate = first_t3 / first_n * 100 if first_n >= 5 else None
    second_rate = second_t3 / second_n * 100 if second_n >= 5 else None

    stable_patterns.append({
        'key': key, 'n': total_n, 't3r': t3r, 'roi': roi,
        'wins': total_wins, 'top3': total_t3,
        'stability': stability, 'n_years': len(years_with_data),
        'years_above_30': years_above_30,
        'first_rate': first_rate, 'second_rate': second_rate,
        'yearly': yearly,
    })

print(f"\n全429パターンのうち:")
print(f"  3年以上データあり: {len(stable_patterns)}件")

# 安定性でランク分け
very_stable = [p for p in stable_patterns if p['stability'] >= 0.7 and p['n'] >= 30]
moderate = [p for p in stable_patterns if 0.5 <= p['stability'] < 0.7 and p['n'] >= 30]
unstable = [p for p in stable_patterns if p['stability'] < 0.5]

print(f"  安定(70%以上の年で複勝率30%+ & 30件以上): {len(very_stable)}件")
print(f"  やや安定(50-70%): {len(moderate)}件")
print(f"  不安定(50%未満): {len(unstable)}件")

# サンプル50件以上の堅いパターン
big_sample = [p for p in stable_patterns if p['n'] >= 50 and p['stability'] >= 0.6]
print(f"  50件以上 & 安定度60%+: {len(big_sample)}件")

# 安定パターンTOP20
print(f"\n--- 安定パターン TOP20（安定度×件数順）---")
very_stable.sort(key=lambda x: (-x['stability'], -x['n']))
print(f"{'#':>3} {'会場':>4} {'距離':>5} {'父':>18} {'件数':>4} {'複勝率':>6} {'単回':>6} {'安定度':>5} {'前半':>5} {'後半':>5} 年別")
for i, p in enumerate(very_stable[:20], 1):
    k = p['key']
    yr_detail = ' '.join(f"{yr}:{v[1]}/{v[0]}" for yr, v in sorted(p['yearly'].items()) if v[0] >= 2)
    fr = f"{p['first_rate']:.0f}%" if p['first_rate'] else "--"
    sr = f"{p['second_rate']:.0f}%" if p['second_rate'] else "--"
    print(f"{i:>3} {k[0]:>4} {k[1]:>5}m {k[2]:>18} {p['n']:>4} {p['t3r']:>5.1f}% {p['roi']:>5.1f}% {p['stability']:>4.0%} {fr:>5} {sr:>5} {yr_detail}")

# ── ② 既存score_bloodlineとの差分 ──
print("\n" + "=" * 80)
print("検証2: 既存score_bloodline(surface×距離帯)との差分")
print("=" * 80)

# 既存のbloodline_statsの粒度: sire × surface × dist_bucket(短/中/長)
# 新データの粒度: sire × venue × distance
# → 「全体では平凡だが特定会場で異常に強い」パターンを探す

# まずsire × surface × 距離帯の全体複勝率を計算
sire_surface = defaultdict(lambda: [0, 0])
for r in rows:
    dist_bucket = '短' if r['distance'] < 1400 else '中' if r['distance'] < 2000 else '長'
    key = (r['sire'], r['surface'], dist_bucket)
    sire_surface[key][0] += 1
    if r['finish'] <= 3: sire_surface[key][1] += 1

# 会場単位で「全体平均との乖離」が大きいパターンを抽出
print(f"\n--- 全体平均より+15pt以上高い会場×距離×父 (50件以上) ---")
print(f"{'#':>3} {'会場':>4} {'距離':>5} {'父':>18} {'件数':>4} {'会場複勝率':>8} {'全体複勝率':>8} {'差':>6} {'単回':>6}")

divergent = []
for p in stable_patterns:
    if p['n'] < 30: continue
    k = p['key']
    venue, dist, sire = k
    # この父の全体複勝率を取得
    surface_row = [r for r in rows if r['sire'] == sire and r['venue'] == venue]
    if not surface_row: continue
    surface = surface_row[0]['surface']
    dist_bucket = '短' if dist < 1400 else '中' if dist < 2000 else '長'
    overall_key = (sire, surface, dist_bucket)
    if overall_key not in sire_surface: continue
    overall_n, overall_t3 = sire_surface[overall_key]
    if overall_n < 20: continue
    overall_rate = overall_t3 / overall_n * 100
    diff = p['t3r'] - overall_rate
    if diff >= 15:
        divergent.append({**p, 'overall_rate': overall_rate, 'diff': diff, 'surface': surface})

divergent.sort(key=lambda x: -x['diff'])
for i, d in enumerate(divergent[:25], 1):
    k = d['key']
    print(f"{i:>3} {k[0]:>4} {k[1]:>5}m {k[2]:>18} {d['n']:>4} {d['t3r']:>7.1f}% {d['overall_rate']:>7.1f}% {d['diff']:>+5.1f}pt {d['roi']:>5.1f}%")

print(f"\n全体平均との乖離+15pt以上: {len(divergent)}件")

# ── ③ 枠番×父 と 馬場×父 の安定性 ──
print("\n" + "=" * 80)
print("検証3: 枠番グループ×父 / 馬場×父 の年度安定性")
print("=" * 80)

def gate_group(umaban, num_horses):
    ratio = umaban / num_horses
    if ratio <= 0.375: return '内(1-3枠)'
    elif ratio <= 0.750: return '中(4-6枠)'
    else: return '外(7-8枠)'

# 枠番×父
gs_yearly = defaultdict(lambda: defaultdict(lambda: [0, 0]))
for r in rows:
    gg = gate_group(r['umaban'], r['num_horses'])
    key = (gg, r['sire'])
    yr = r['date'][:4]
    gs_yearly[key][yr][0] += 1
    if r['finish'] <= 3: gs_yearly[key][yr][1] += 1

print("\n--- 枠番×父 安定パターン (15件+, 安定度70%+) ---")
gate_stable = []
for key, yearly in gs_yearly.items():
    total_n = sum(v[0] for v in yearly.values())
    total_t3 = sum(v[1] for v in yearly.values())
    if total_n < 15: continue
    t3r = total_t3 / total_n * 100
    total_odds = 0
    for r in rows:
        gg = gate_group(r['umaban'], r['num_horses'])
        if (gg, r['sire']) == key and r['finish'] == 1:
            total_odds += r['odds']
    roi = total_odds / total_n * 100
    if not (t3r >= 80 or roi >= 150): continue
    years_data = [(yr, v) for yr, v in yearly.items() if v[0] >= 2]
    if len(years_data) < 3: continue
    stab = sum(1 for yr, v in years_data if v[1] / v[0] * 100 >= 25) / len(years_data)
    if stab >= 0.6:
        gate_stable.append({'key': key, 'n': total_n, 't3r': t3r, 'roi': roi, 'stab': stab})

gate_stable.sort(key=lambda x: (-x['stab'], -x['t3r']))
print(f"{'#':>3} {'枠':>10} {'父':>18} {'件数':>4} {'複勝率':>6} {'単回':>6} {'安定度':>5}")
for i, g in enumerate(gate_stable[:15], 1):
    print(f"{i:>3} {g['key'][0]:>10} {g['key'][1]:>18} {g['n']:>4} {g['t3r']:>5.1f}% {g['roi']:>5.1f}% {g['stab']:>4.0%}")

# 馬場×父
ts_yearly = defaultdict(lambda: defaultdict(lambda: [0, 0, 0.0]))
for r in rows:
    cond = r['track_cond']
    if cond not in ('重', '不'): continue
    cond_label = '重' if cond == '重' else '不良'
    key = (cond_label, r['sire'])
    yr = r['date'][:4]
    ts_yearly[key][yr][0] += 1
    if r['finish'] <= 3: ts_yearly[key][yr][1] += 1
    if r['finish'] == 1: ts_yearly[key][yr][2] += r['odds']

print("\n--- 馬場(重/不良)×父 安定パターン (10件+, 安定度60%+) ---")
track_stable = []
for key, yearly in ts_yearly.items():
    total_n = sum(v[0] for v in yearly.values())
    total_t3 = sum(v[1] for v in yearly.values())
    total_odds = sum(v[2] for v in yearly.values())
    if total_n < 10: continue
    t3r = total_t3 / total_n * 100
    roi = total_odds / total_n * 100
    if not (t3r >= 80 or roi >= 150): continue
    years_data = [(yr, v) for yr, v in yearly.items() if v[0] >= 2]
    if len(years_data) < 2: continue
    stab = sum(1 for yr, v in years_data if v[1] / v[0] * 100 >= 25) / len(years_data)
    if stab >= 0.5:
        track_stable.append({'key': key, 'n': total_n, 't3r': t3r, 'roi': roi, 'stab': stab})

track_stable.sort(key=lambda x: (-x['stab'], -x['t3r']))
print(f"{'#':>3} {'馬場':>4} {'父':>18} {'件数':>4} {'複勝率':>6} {'単回':>6} {'安定度':>5}")
for i, t in enumerate(track_stable[:15], 1):
    print(f"{i:>3} {t['key'][0]:>4} {t['key'][1]:>18} {t['n']:>4} {t['t3r']:>5.1f}% {t['roi']:>5.1f}% {t['stab']:>4.0%}")

# ── サマリ ──
print("\n" + "=" * 80)
print("検証サマリ")
print("=" * 80)
print(f"  ① 会場×距離×父:")
print(f"     全パターン: 429件")
print(f"     安定(70%+ & 30件+): {len(very_stable)}件")
print(f"     50件以上&安定60%+: {len(big_sample)}件")
print(f"     全体平均から+15pt乖離: {len(divergent)}件 ← これが新しい情報")
print(f"  ② 枠番×父 安定パターン: {len(gate_stable)}件")
print(f"  ③ 馬場×父 安定パターン: {len(track_stable)}件")
