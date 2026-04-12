"""
backtest_v7.py — v6条件 + 未勝利復活版（JTスコア未勝利限定勝率対応）

v6の買い条件ベース + 未勝利2条件を追加:
  未勝利: (odds 12-20 & gap 8-12) OR (odds 16-20 & heads 14+)
"""

import sys, os, time, json, sqlite3, shutil, numpy as np
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, '.')

import importlib, scoring
importlib.reload(scoring)

from scoring import get_conn
from backtest_2026 import prefetch_month, clear_caches
from backtest_full import prefetch_jt, prefetch_score_caches, score_one_race, grade_full
from backtest_v2 import calc_win_prob_s12, calc_ev_scale7, _get_finish, _get_div_cached, _div_cache
from backtest_v3 import get_payout_v3, summarize_v3


def is_buy_v7(grade, heads, gap, odds, ev7):
    """v6 + 未勝利2条件"""
    if odds is None or odds == 0: return False
    if not (2.0 <= ev7 <= 20.0): return False

    if grade == '新馬': return False
    if grade == '未勝利':
        return (12 <= odds <= 20 and 8 <= gap <= 12) or \
               (16 <= odds <= 20 and heads >= 14)
    if grade == '1勝': return 3 <= odds <= 5
    if grade == '2勝': return 20 <= odds <= 30
    if grade == '3勝':
        return (5 <= odds <= 20 and heads >= 12) or \
               (8 <= odds <= 20 and gap >= 8)
    if grade == 'G3': return 3 <= odds <= 16 and 3 <= gap <= 8 and heads >= 14
    return False


def run_month_v7(conn, sc_conn, year, month):
    d_from = f'{year}-{month:02d}-01'
    d_to = f'{year}-{month:02d}-31'
    races = conn.execute(
        'SELECT DISTINCT date,venue,race_num FROM results '
        'WHERE date BETWEEN ? AND ? ORDER BY date,venue,race_num',
        (d_from, d_to)
    ).fetchall()

    all_races = []; bets = []
    for race in races:
        rows = [dict(r) for r in conn.execute(
            'SELECT * FROM results WHERE date=? AND venue=? AND race_num=? ORDER BY horse_num',
            (race['date'], race['venue'], race['race_num'])
        ).fetchall()]
        if len(rows) < 3: continue
        rname = rows[0].get('race_name', '')
        if '障害' in str(rname): continue

        gr = grade_full(rname)
        try:
            result, meta = score_one_race(rows, sc_conn)
        except: continue
        if not result or len(result) < 2: continue

        honmei = result[0]; ni = result[1]
        scores = [h['total_score'] for h in result]
        win_prob = calc_win_prob_s12(scores)
        gap = meta['standout_gap']
        honmei_odds = honmei.get('odds')

        ev7 = calc_ev_scale7(scores, honmei_odds or 0)
        ev_ok = is_buy_v7(gr, len(result), gap, honmei_odds or 0, ev7)

        honmei_ev = win_prob * (honmei_odds or 0)
        winner_rank = next((h['rank'] for h in result if h['finish'] == 1), None)

        rec = {
            'date': race['date'], 'venue': race['venue'],
            'race_num': race['race_num'], 'grade': gr,
            'heads': len(rows), 'gap': round(gap, 1),
            'honmei_name': honmei['horse_name'],
            'honmei_num': honmei['horse_num'],
            'honmei_odds': honmei_odds,
            'honmei_ev': round(honmei_ev, 3),
            'win_prob_pct': round(win_prob * 100, 1),
            'honmei_finish': honmei['finish'],
            'ni_name': ni['horse_name'],
            'ni_finish': ni['finish'],
            'winner_rank': winner_rank,
            'actual_win': (honmei['finish'] == 1),
            'ev_ok': ev_ok,
        }
        all_races.append(rec)
        if not ev_ok: continue

        d = race['date']; v = race['venue']; rn = race['race_num']
        pay_t, pay_u, pay_w, hits = get_payout_v3(
            conn, d, v, rn, honmei['horse_name'], ni['horse_name'])

        ret = pay_t + pay_u + pay_w
        bets.append({**rec, 'ret': ret, 'profit': ret - 1000,
                     'hits': ' + '.join(hits) if hits else '全外れ'})

    return all_races, bets


def run_year_v7(year, db_path):
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

        ar, br = run_month_v7(conn, sc_conn, year, month)
        all_races += ar
        bet_records += br
        sys.stdout.write(f'\r  {year}/{month:02d} 全{len(ar)}R 買{len(br)}R 累{time.time()-t0:.0f}s  ')
        sys.stdout.flush()
    print()
    conn.close(); sc_conn.close()
    return all_races, bet_records


if __name__ == '__main__':
    if '--year' not in sys.argv:
        print("Usage: python backtest_v7.py --year YYYY")
        sys.exit(1)

    idx = sys.argv.index('--year')
    year = int(sys.argv[idx + 1])

    src_db = 'keiba.db'
    tmp_db = f'keiba_tmp_{year}.db'
    if Path(f'{src_db}-wal').exists():
        c = sqlite3.connect(src_db)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
    shutil.copy2(src_db, tmp_db)

    print(f'\n{"="*55}')
    print(f'  backtest_v7 [v6+未勝利+JT改修]  {year}年')
    print(f'{"="*55}')

    t_start = time.time()
    all_races, bet_records = run_year_v7(year, tmp_db)
    elapsed = time.time() - t_start

    s = summarize_v3(year, all_races, bet_records)
    s['elapsed_sec'] = round(elapsed, 1)

    fname = f'btv7_{year}.json'
    with open(fname, 'w', encoding='utf-8') as f:
        json.dump({'summary': s, 'bet_records': bet_records}, f,
                  ensure_ascii=False, default=str)

    Path(tmp_db).unlink(missing_ok=True)

    print(f'\n  -- {year}年 結果 (v7) --')
    print(f'  買い: {s["n_bet"]}R / 全{s["total_races"]}R  ({elapsed:.0f}s)')
    print(f'  損益: {s["profit"]:+,}円   ROI: {s["roi"]}%')
    print(f'  グレード別:')
    for g in ['未勝利','1勝','2勝','3勝','G3']:
        if g in s['grade_detail']:
            v = s['grade_detail'][g]
            print(f'    {g}: {v["n"]}R  ROI={v["roi"]}%  損益{v["profit"]:+,}円')
    print(f'  -> {fname} 保存済み')
