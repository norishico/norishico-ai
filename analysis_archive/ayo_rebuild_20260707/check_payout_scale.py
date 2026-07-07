# -*- coding: utf-8 -*-
"""dividends の払戻カラムの単位（円 vs 10円単位）を実測で確定する。
単勝: 勝ち馬の確定オッズ×100円 と tansho_payout を比較すれば一意に決まる。
複勝: 1番人気馬が3着内のときの fukusho payout の中央値で判定（円なら110-300、10円単位なら11-30）。
馬連/ワイド: 中央値の桁で判定。
"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

conn = sqlite3.connect(r'file:C:\Users\westr\norishiko_ai\keiba.db?mode=ro', uri=True)

# --- 単勝: odds×100 との比率 ---
rows = conn.execute("""
    SELECT r.odds, d.tansho_payout
    FROM results r JOIN dividends d ON d.race_id = r.race_id AND d.tansho_umaban = r.umaban
    WHERE r.finish = 1 AND r.odds > 0 AND d.tansho_payout > 0
      AND r.date BETWEEN '2024-01-01' AND '2025-12-31'
    LIMIT 2000
""").fetchall()
ratios = [p / (o * 100) for o, p in rows if o > 0]
ratios.sort()
n = len(ratios)
print(f'単勝: n={n}  payout/(odds*100) 中央値={ratios[n//2]:.3f}  P10={ratios[n//10]:.3f}  P90={ratios[9*n//10]:.3f}')
print('  → 中央値≈1.0なら「円」、≈0.1なら「10円単位」')

# --- 複勝: 1番人気3着内の払戻分布 ---
rows = conn.execute("""
    SELECT d.fukusho1_payout FROM dividends d
    JOIN results r ON r.race_id = d.race_id AND r.umaban = d.fukusho1_umaban
    WHERE r.popularity = 1 AND d.fukusho1_payout > 0
      AND d.date BETWEEN '2024-01-01' AND '2025-12-31'
    LIMIT 2000
""").fetchall()
vals = sorted(v[0] for v in rows)
n = len(vals)
if n:
    print(f'複勝(1人気): n={n}  中央値={vals[n//2]}  P10={vals[n//10]}  P90={vals[9*n//10]}')
    print('  → 中央値が110-200なら「円」、11-20なら「10円単位」')

# --- 馬連 / ワイド: 分布の桁 ---
for col in ('umaren_payout', 'wide1_payout', 'sanrenpuku_payout'):
    rows = conn.execute(f"""
        SELECT {col} FROM dividends WHERE {col} > 0
          AND date BETWEEN '2024-01-01' AND '2025-12-31' LIMIT 2000
    """).fetchall()
    vals = sorted(v[0] for v in rows)
    n = len(vals)
    if n:
        print(f'{col}: n={n}  中央値={vals[n//2]}  P10={vals[n//10]}  P90={vals[9*n//10]}')

conn.close()
