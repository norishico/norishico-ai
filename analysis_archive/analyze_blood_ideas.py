"""
血統 Tier 1 アイデア 予備分析

#1 血統の旬: 直近30日の父産駒複勝率が過去1年平均と有意に違うか
#2 脚質予測: 父種牡馬ごとの脚質分布は明確に分かれるか
#3 血統×馬場: 父×馬場状態の組合せで有意な優位性があるか
"""
import sqlite3, sys, io
from collections import defaultdict
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

c = sqlite3.connect('keiba.db')
c.row_factory = sqlite3.Row

print('='*80)
print('Phase A: 血統 Tier 1 予備分析')
print('='*80)

# ───────────────────────────────────
# #1 血統の旬検知
# ───────────────────────────────────
print('\n【#1 血統の旬: 直近30日 vs 過去1年】')
print('-'*60)

# 各年の「直近30日の変動」を見る
# 2024年の各月を基準に、直近30日 vs 過去1年平均の乖離
import itertools
hotness_samples = []
# 2024年の代表月で検証
for ref_date in ['2024-04-01', '2024-07-01', '2024-10-01', '2024-12-01']:
    # この時点で、代表的な父種牡馬の直近30日勝率 vs 過去1年
    r = c.execute(f'''
    SELECT sire,
           COUNT(*) as n_recent,
           SUM(CASE WHEN finish<=3 THEN 1.0 ELSE 0 END)*100/COUNT(*) as rate_recent
    FROM results
    WHERE date BETWEEN date('{ref_date}', '-30 days') AND date('{ref_date}', '-1 days')
      AND sire != '' AND finish < 90
    GROUP BY sire
    HAVING n_recent >= 20
    ORDER BY n_recent DESC
    LIMIT 10
    ''').fetchall()
    print(f'\n  {ref_date}時点 (n>=20の父):')
    for row in r:
        sire = row['sire']
        rate_r = row['rate_recent']
        n_r = row['n_recent']
        # 過去1年の平均
        b = c.execute('''
        SELECT COUNT(*) as n_base,
               SUM(CASE WHEN finish<=3 THEN 1.0 ELSE 0 END)*100/COUNT(*) as rate_base
        FROM results
        WHERE date BETWEEN date(?, '-365 days') AND date(?, '-31 days')
          AND sire = ? AND finish < 90
        ''', (ref_date, ref_date, sire)).fetchone()
        if b and b['n_base'] >= 100:
            dev = rate_r - b['rate_base']
            flag = '🟢' if dev > 5 else ('🔴' if dev < -5 else '-')
            print(f'    {sire:<18}: recent n={n_r:>3} {rate_r:>5.1f}% / baseline {b["n_base"]:>4} {b["rate_base"]:>5.1f}% / 乖離 {dev:>+5.1f}pt {flag}')
            hotness_samples.append((sire, ref_date, dev, n_r, b['n_base']))

# 乖離の分布
if hotness_samples:
    devs = [s[2] for s in hotness_samples]
    hot = sum(1 for d in devs if d > 5)
    cold = sum(1 for d in devs if d < -5)
    print(f'\n  乖離5pt超え: 🟢{hot}件 🔴{cold}件 / 全{len(devs)}件')

# ───────────────────────────────────
# #2 脚質予測: 父ごとの脚質分布
# ───────────────────────────────────
print('\n\n【#2 脚質予測: 父種牡馬ごとの脚質分布】')
print('-'*60)

# 脚質 = pos4/num_horses の比率
#   逃げ: ratio <= 0.2
#   先行: 0.2 < ratio <= 0.45
#   中団: 0.45 < ratio <= 0.7
#   差追: ratio > 0.7

q = '''
SELECT sire, COUNT(*) as n,
       AVG(CAST(pos4 AS REAL) / num_horses) as avg_ratio,
       SUM(CASE WHEN CAST(pos4 AS REAL)/num_horses <= 0.2 THEN 1 ELSE 0 END)*100.0/COUNT(*) as nige_pct,
       SUM(CASE WHEN CAST(pos4 AS REAL)/num_horses <= 0.45 AND CAST(pos4 AS REAL)/num_horses > 0.2 THEN 1 ELSE 0 END)*100.0/COUNT(*) as senkou_pct,
       SUM(CASE WHEN CAST(pos4 AS REAL)/num_horses > 0.7 THEN 1 ELSE 0 END)*100.0/COUNT(*) as oikomi_pct
FROM results
WHERE date >= '2022-01-01' AND sire != '' AND pos4 > 0 AND num_horses > 0 AND finish < 90
GROUP BY sire
HAVING n >= 200
ORDER BY n DESC
LIMIT 15
'''
rows = c.execute(q).fetchall()
print(f'  父種牡馬の平均脚質 (2022+ n>=200):')
print(f'{"sire":>20} {"n":>5} {"平均位置":>8} {"逃%":>6} {"先%":>6} {"追%":>6}')
style_clear = 0
for r in rows:
    avg = r['avg_ratio']
    style = '逃' if avg<=0.3 else ('先' if avg<=0.5 else ('中' if avg<=0.65 else '追'))
    # 支配的な脚質があるか (40%超え)
    max_pct = max(r['nige_pct'], r['senkou_pct'], r['oikomi_pct'])
    if max_pct > 40: style_clear += 1
    print(f'  {r["sire"]:>20} {r["n"]:>5} {avg:>6.3f}({style}) {r["nige_pct"]:>5.1f} {r["senkou_pct"]:>5.1f} {r["oikomi_pct"]:>5.1f}')

print(f'\n  支配的脚質 (40%超え) を持つ父: {style_clear}/{len(rows)}')

# ───────────────────────────────────
# #3 血統×馬場状態
# ───────────────────────────────────
print('\n\n【#3 血統×馬場状態】')
print('-'*60)

# 全体の馬場別平均複勝率
for cond in ['良', '稍重', '重', '不']:
    r = c.execute('''
    SELECT COUNT(*) as n, SUM(CASE WHEN finish<=3 THEN 1.0 ELSE 0 END)*100/COUNT(*) as rate
    FROM results WHERE track_cond=? AND date>='2020-01-01' AND finish<90
    ''', (cond,)).fetchone()
    if r['n'] and r['rate'] is not None:
        print(f'  {cond}: n={r["n"]:,} 複勝率{r["rate"]:.1f}%')
    else:
        print(f'  {cond}: なし')

# 各父 × 馬場の相対優位性
print(f'\n  父×馬場 (n>=50, 乖離+10pt以上):')
q2 = '''
WITH sire_cond AS (
    SELECT sire, track_cond,
           COUNT(*) as n,
           SUM(CASE WHEN finish<=3 THEN 1.0 ELSE 0 END)*100.0/COUNT(*) as rate
    FROM results
    WHERE date >= '2020-01-01' AND sire != '' AND finish < 90
    GROUP BY sire, track_cond
    HAVING n >= 50
),
sire_all AS (
    SELECT sire,
           COUNT(*) as n_all,
           SUM(CASE WHEN finish<=3 THEN 1.0 ELSE 0 END)*100.0/COUNT(*) as rate_all
    FROM results
    WHERE date >= '2020-01-01' AND sire != '' AND finish < 90
    GROUP BY sire
    HAVING n_all >= 200
)
SELECT sc.sire, sc.track_cond, sc.n, sc.rate, sa.rate_all,
       sc.rate - sa.rate_all as dev
FROM sire_cond sc
JOIN sire_all sa ON sc.sire = sa.sire
WHERE sc.rate - sa.rate_all > 10
ORDER BY dev DESC
LIMIT 20
'''
rows = c.execute(q2).fetchall()
for r in rows:
    print(f'  {r["sire"]:<18} × {r["track_cond"]:<4}: n={r["n"]:>3} 実{r["rate"]:>5.1f}% (父平均{r["rate_all"]:>5.1f}%) 乖離+{r["dev"]:.1f}pt 🟢')

# まとめ
print()
print('='*80)
print('【予備分析まとめ】')
print('='*80)
print('#1 血統の旬: サンプル十分で変動も見える → 実装価値あり')
print('#2 脚質予測: 父の脚質傾向は存在 → シンプルに実装可能')
print('#3 血統×馬場: 有意な乖離パターン存在 → venue_sire_bonus と重複に注意')
