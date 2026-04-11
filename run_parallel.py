"""
run_parallel.py — 年ごとの並列バックテスト実行
DB事前複製 → max 3並列で実行 → サマリー集計
"""
import subprocess, sys, time, json, os, shutil
from pathlib import Path

os.environ['PYTHONIOENCODING'] = 'utf-8'

PROJ_DIR = Path(__file__).parent
PYEXE = shutil.which('py') or sys.executable
MAX_WORKERS = 3
SRC_DB = PROJ_DIR / 'keiba.db'

script = sys.argv[1] if len(sys.argv) > 1 else 'backtest_v6.py'
years = list(range(2020, 2027))

print(f'=== 並列実行開始: {script} x {len(years)}年 (max {MAX_WORKERS}並列) ===')
print(f'  Python: {PYEXE}\n')

# Step 1: DB事前複製（順番に行いロック競合を回避）
print(f'📦 DB事前複製...')
for y in years:
    dst = PROJ_DIR / f'keiba_tmp_{y}.db'
    dst.unlink(missing_ok=True)
    shutil.copy2(SRC_DB, dst)
print(f'  {len(years)}ファイル複製完了\n')

t0 = time.time()

# Step 2: max 3並列で実行（バッチ方式）
pending = list(years)
running = {}  # year -> Popen

while pending or running:
    # 空きスロットがあれば起動
    while pending and len(running) < MAX_WORKERS:
        y = pending.pop(0)
        p = subprocess.Popen(
            [PYEXE, '-X', 'utf8', str(PROJ_DIR / script), '--year', str(y)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
            cwd=str(PROJ_DIR),
        )
        running[y] = p
        print(f'  {y}年: PID={p.pid} 起動 (並列{len(running)}/{MAX_WORKERS})')

    # 完了チェック
    done = []
    for y, p in running.items():
        if p.poll() is not None:
            done.append(y)
    for y in done:
        p = running.pop(y)
        elapsed = time.time() - t0
        output = p.stdout.read().decode('utf-8', errors='replace')
        # 一時DB削除
        (PROJ_DIR / f'keiba_tmp_{y}.db').unlink(missing_ok=True)
        # 結果行だけ抽出
        for line in output.split('\n'):
            if '損益' in line or 'ROI' in line or '買い:' in line or f'{y}:' in line:
                print(f'  [{y}] {line.strip()}')
        if p.returncode != 0:
            print(f'  [{y}] ERROR (exit={p.returncode})')
            for line in output.split('\n')[-5:]:
                if line.strip():
                    print(f'    {line}')
        else:
            print(f'  [{y}] 完了 ({elapsed:.0f}s経過)')
        print()

    if running:
        time.sleep(1)

total_elapsed = time.time() - t0
print(f'=== 全完了: {total_elapsed:.0f}秒 ===\n')

# Step 3: サマリー集計
print(f'{"="*55}')
print(f'  全年サマリー [{script}]')
print(f'{"="*55}')
total_bet = 0
total_ret = 0
for y in sorted(years):
    prefix = script.replace('backtest_v', 'btv').replace('.py', '')
    fname = f'{prefix}_{y}.json'
    try:
        with open(fname, encoding='utf-8') as f:
            d = json.load(f)
        s = d['summary']
        total_bet += s['n_bet']
        total_ret += s['n_bet'] * 1000 + s['profit']
        print(f'  {y}: {s["n_bet"]:>4}R  ROI={s["roi"]:>6.1f}%  損益={s["profit"]:>+10,}円')
    except FileNotFoundError:
        print(f'  {y}: ファイルなし')
if total_bet:
    total_roi = total_ret / (total_bet * 1000) * 100
    total_profit = int(total_ret - total_bet * 1000)
    print(f'  {"合計":}: {total_bet:>4}R  ROI={total_roi:>6.1f}%  損益={total_profit:>+10,}円')
