"""馬場別ROI分析 — v6買いレースの馬場状態別成績
run_parallel.py経由で並列実行可能:
  python run_parallel.py analyze_track_cond.py
単年実行:
  python analyze_track_cond.py --year 2024
"""
import sqlite3, sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import importlib, scoring
importlib.reload(scoring)
from scoring import get_conn
from backtest_2026 import prefetch_month, clear_caches
from backtest_full import prefetch_jt, prefetch_score_caches, score_one_race, grade_full
from backtest_v2 import calc_win_prob_s12, calc_ev_scale7, _get_div_cached, _div_cache
from backtest_v3 import get_payout_v3
from backtest_v6 import is_buy_v6, SUNDAY_SIRES

DB = os.environ.get('KEIBA_DB', 'keiba.db')

def run_year(year):
    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")
    sc = sqlite3.connect(DB); sc.row_factory = sqlite3.Row
    sc.execute("PRAGMA cache_size=-65536")

    cutoff = f'{year}-01-01'
    prefetch_score_caches(sc, cutoff_date=cutoff)

    results = []
    for month in range(1, 13):
        if year == 2026 and month > 3: break
        try:
            clear_caches(full=False)
            prefetch_month(conn, year, month)
            prefetch_jt(conn, year, month)
        except: continue

        d_from = f'{year}-{month:02d}-01'
        d_to = f'{year}-{month:02d}-31'
        races = conn.execute(
            'SELECT DISTINCT date,venue,race_num FROM results WHERE date BETWEEN ? AND ? ORDER BY date,venue,race_num',
            (d_from, d_to)).fetchall()

        for race in races:
            rows = [dict(r) for r in conn.execute(
                'SELECT * FROM results WHERE date=? AND venue=? AND race_num=? ORDER BY horse_num',
                (race['date'], race['venue'], race['race_num'])).fetchall()]
            if len(rows) < 3: continue
            rname = rows[0].get('race_name', '')
            if '障害' in str(rname): continue

            cond = rows[0].get('track_cond', '良') or '良'
            gr = grade_full(rname)

            try:
                result, meta = score_one_race(rows, sc)
            except: continue
            if not result or len(result) < 2: continue

            honmei = result[0]; ni = result[1]
            scores = [h['total_score'] for h in result]
            gap = meta['standout_gap']
            odds = honmei.get('odds') or 0
            ev7 = calc_ev_scale7(scores, odds)
            good = honmei.get('has_good_train', False)
            sire = honmei.get('_sire', '')

            zone, ok = is_buy_v6(gr, len(result), gap, odds, ev7, good_train=good, sire=sire)
            if not ok: continue

            d = race['date']; v = race['venue']; rn = race['race_num']
            div = _get_div_cached(conn, d, v, rn)

            if zone == 'challenge':
                cost = 1000
                ret = (div['tansho_payout'] * 10) if honmei['finish'] == 1 and div else 0
            else:
                cost = 2000
                t, u, w, _ = get_payout_v3(conn, d, v, rn, honmei['horse_name'], ni['horse_name'])
                ret = t + u + w

            results.append({
                'cond': cond, 'cost': cost, 'ret': ret,
                'finish': honmei['finish'], 'grade': gr,
            })

    conn.close(); sc.close()
    return results

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--year', type=int, required=True)
    args = parser.parse_args()

    t0 = time.time()
    data = run_year(args.year)
    elapsed = time.time() - t0

    # 馬場別集計
    by_cond = {}
    for d in data:
        c = d['cond']
        if c not in by_cond: by_cond[c] = {'n': 0, 'cost': 0, 'ret': 0, 'wins': 0}
        by_cond[c]['n'] += 1
        by_cond[c]['cost'] += d['cost']
        by_cond[c]['ret'] += d['ret']
        if d['finish'] == 1: by_cond[c]['wins'] += 1

    total_cost = sum(v['cost'] for v in by_cond.values())
    total_ret = sum(v['ret'] for v in by_cond.values())
    total_n = sum(v['n'] for v in by_cond.values())
    roi = total_ret / total_cost * 100 if total_cost > 0 else 0
    profit = total_ret - total_cost

    print(f'買い:{total_n}R ROI={roi:.1f}% 損益={profit:+,}円')

    # JSON保存
    out = {
        'year': args.year, 'summary': {'n_bet': total_n, 'roi': roi, 'profit': profit},
        'by_cond': by_cond
    }
    fname = f'track_cond_{args.year}.json'
    with open(fname, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
