import sys
import sqlite3
sys.path.insert(0, 'nar')
import backtest_nar

db_path = sys.argv[1]
conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
conn.execute('PRAGMA query_only=1')
backtest_nar.run_walkforward(conn)
conn.close()
