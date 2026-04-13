"""最新の調教CSVをtrainingテーブルに追加投入"""
import sqlite3
import glob
from build_db import parse_training_csv

conn = sqlite3.connect('keiba.db')

# 現在の最新日付
latest = conn.execute('SELECT MAX(date) FROM training').fetchone()[0]
print(f"現在の最新: {latest}")

# 4/1と4/3のCSVを投入
files = [
    '20260401_坂路調教_2019-2026.csv',
    '20260401_ｳｯﾄﾞﾁｯﾌﾟ調教_2019-2026.csv',
    '20260403_坂路調教.csv',
    '20260403_ｳｯﾄﾞﾁｯﾌﾟ調教.csv',
]

total_added = 0
for f in files:
    try:
        print(f"\n処理中: {f}")
        df = parse_training_csv(f)
        # 既存データより新しいものだけ投入
        df = df[df['date'] > latest]
        if df.empty:
            print(f"  → 新規データなし")
            continue

        cols = ['horse_name', 'date', 'trainer', 'venue', 'source',
                'lap1', 'lap2', 'time3', 'time4', 'sire', 'dam', 'sex', 'age']
        existing_cols = [c for c in cols if c in df.columns]

        for _, row in df.iterrows():
            vals = [row.get(c) for c in existing_cols]
            placeholders = ','.join(['?'] * len(existing_cols))
            col_names = ','.join(existing_cols)
            conn.execute(f"INSERT OR IGNORE INTO training ({col_names}) VALUES ({placeholders})", vals)

        conn.commit()
        print(f"  → {len(df)}件追加")
        total_added += len(df)
    except Exception as e:
        print(f"  → エラー: {e}")

# 確認
new_latest = conn.execute('SELECT MAX(date) FROM training').fetchone()[0]
total = conn.execute('SELECT COUNT(*) FROM training').fetchone()[0]
print(f"\n=== 完了 ===")
print(f"追加: {total_added}件")
print(f"最新日付: {latest} → {new_latest}")
print(f"合計: {total:,}件")

# 追加分の日付別件数
print("\n追加分の日付別:")
for r in conn.execute(f"SELECT date, COUNT(*) as n FROM training WHERE date > '{latest}' GROUP BY date ORDER BY date").fetchall():
    print(f"  {r[0]}: {r[1]}件")

conn.close()
