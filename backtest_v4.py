"""
backtest_v4.py — v4重み版バックテスト

v3の4施策 + 重み係数v4を使用
DB競合回避: 各年プロセスが独自のDBコピーを使用
"""

import sys, os, time, json, sqlite3, shutil, numpy as np
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, '.')

# scoring.pyを先にリロード（v4重み反映）
import importlib
import scoring
importlib.reload(scoring)

from scoring import get_conn
from backtest_2026 import prefetch_month, clear_caches
from backtest_full import prefetch_jt, prefetch_score_caches, score_one_race, grade_full
from backtest_v2 import calc_win_prob_s12, calc_ev_scale7, _get_finish, _get_div_cached, _div_cache
from backtest_v3 import is_buy_v3, get_payout_v3, run_month_v3, summarize_v3


def run_year_v4(year, db_path):
    """1年分をDB別コピーで実行（並列安全）"""
    conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    sc_conn = sqlite3.connect(db_path); sc_conn.row_factory = sqlite3.Row
    sc_conn.execute("PRAGMA cache_size=-65536")
    sc_conn.execute("PRAGMA temp_store=MEMORY")

    all_races = []; bet_records = []
    t0 = time.time()
    _div_cache.clear()

    from build_supplementary_tables import (
        build_bloodline_stats, build_gate_cond_blood_bonus, build_track_bias_bonus
    )
    cutoff = f'{year}-01-01'
    build_bloodline_stats(sc_conn, cutoff_date=cutoff)
    build_gate_cond_blood_bonus(sc_conn, cutoff_date=cutoff)
    build_track_bias_bonus(sc_conn, cutoff_date=cutoff)

    scoring._bloodline_score_cache.clear()
    scoring._gcbb_cache.clear(); scoring._gcbb_loaded = False
    scoring._tbb_cache.clear(); scoring._week_cache.clear()

    for month in range(1, 13):
        if month == 1: clear_caches(full=True); prefetch_score_caches(sc_conn, cutoff_date=cutoff)
        else:          clear_caches(full=False)
        prefetch_month(conn, year, month)
        prefetch_jt(conn, year, month)

        ar, br = run_month_v3(conn, sc_conn, year, month)
        all_races += ar
        bet_records += br
        sys.stdout.write(f'\r  {year}/{month:02d} 全{len(ar)}R 買{len(br)}R 累{time.time()-t0:.0f}s  ')
        sys.stdout.flush()
    print()

    conn.close(); sc_conn.close()
    return all_races, bet_records


if __name__ == '__main__':
    if '--year' not in sys.argv:
        print("Usage: python backtest_v4.py --year YYYY")
        sys.exit(1)

    idx = sys.argv.index('--year')
    year = int(sys.argv[idx + 1])

    # DB競合回避: 年ごとに独自コピーを作成
    src_db = 'keiba.db'
    tmp_db = f'keiba_tmp_{year}.db'
    shutil.copy2(src_db, tmp_db)
    # WALファイルも統合
    if Path(f'{src_db}-wal').exists():
        # WALを統合するためにcheckpoint
        c = sqlite3.connect(src_db)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
        shutil.copy2(src_db, tmp_db)

    print(f'\n{"="*55}')
    print(f'  backtest_v4 [v4重み + 4施策]  {year}年')
    print(f'{"="*55}')

    t_start = time.time()
    all_races, bet_records = run_year_v4(year, tmp_db)
    elapsed = time.time() - t_start

    s = summarize_v3(year, all_races, bet_records)
    s['elapsed_sec'] = round(elapsed, 1)

    fname = f'btv4_{year}.json'
    with open(fname, 'w', encoding='utf-8') as f:
        json.dump({'summary': s, 'bet_records': bet_records}, f,
                  ensure_ascii=False, default=str)

    # tmpDB削除
    Path(tmp_db).unlink(missing_ok=True)

    print(f'\n  -- {year}年 結果 (v4重み) --')
    print(f'  買い: {s["n_bet"]}R / 全{s["total_races"]}R  ({elapsed:.0f}s)')
    print(f'  損益: {s["profit"]:+,}円   ROI: {s["roi"]}%')
    print(f'  グレード別:')
    for g in ['未勝利','1勝','2勝','3勝','G3']:
        if g in s['grade_detail']:
            v = s['grade_detail'][g]
            print(f'    {g}: {v["n"]}R  ROI={v["roi"]}%  損益{v["profit"]:+,}円')
    print(f'  -> {fname} 保存済み')
