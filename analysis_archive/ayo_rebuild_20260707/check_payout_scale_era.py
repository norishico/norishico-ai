# -*- coding: utf-8 -*-
"""年代別に tansho_payout / (odds*100) の比率を確認（単位の時代混在チェック）"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

conn = sqlite3.connect(r'file:C:\Users\westr\norishiko_ai\keiba.db?mode=ro', uri=True)

for y in range(2019, 2027):
    rows = conn.execute("""
        SELECT r.odds, d.tansho_payout
        FROM results r JOIN dividends d ON d.race_id = r.race_id AND d.tansho_umaban = r.umaban
        WHERE r.finish = 1 AND r.odds > 0 AND d.tansho_payout > 0
          AND r.date BETWEEN ? AND ?
        LIMIT 1000
    """, (f'{y}-01-01', f'{y}-12-31')).fetchall()
    if not rows:
        print(f'{y}: データなし')
        continue
    ratios = sorted(p / (o * 100) for o, p in rows)
    n = len(ratios)
    print(f'{y}: n={n}  中央値={ratios[n//2]:.3f}  P10={ratios[n//10]:.3f}  P90={ratios[9*n//10]:.3f}')

conn.close()
