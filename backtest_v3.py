"""
backtest_v3.py — 4施策版バックテスト

■ 4施策
  1. ▲関連を廃止（馬連◎▲・ワイド◎▲）
  2. 新馬を買い対象から除外
  3. 買い目再配分: 単勝◎400 + 馬連◎○300 + ワイド◎○300 = 1,000円/R
  4. グレード別オッズフィルタ（分析Aの黒字/惜しいゾーン準拠）

■ 高速化
  - 年ごとにsubprocess並列実行（--year引数で単年実行対応）
"""

import sys, time, json, sqlite3, numpy as np, subprocess, os
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, '.')

import scoring
from scoring import get_conn
from backtest_2026 import prefetch_month, clear_caches
from backtest_full  import prefetch_jt, prefetch_score_caches, score_one_race, grade_full
from backtest_v2 import (
    calc_win_prob_s12, calc_ev_scale7,
    get_payout_v2, _div_cache, _get_finish,
    _get_div_cached,
)

# ── 施策4: グレード別オッズフィルタ付き買い条件 ────────────
def is_buy_v3(grade, heads, gap, odds, ev7):
    """
    4施策版の買い条件:
    - 新馬: 除外
    - 未勝利: heads>=10, odds 10-20 (分析A: 12-19.9帯ROI99.7%)
    - 1勝: heads>=10, odds 3-8 (分析A: 3-4.9帯ROI98%)
    - 2勝: heads>=10, odds 15-30 (分析A: 20-30帯ROI102%)
    - 3勝: heads>=8, odds 5-20 (分析A: 5-20帯ROI105-157%)
    - G3: heads>=10, gap>=3, odds 8-20 (分析A: 12-19.9帯ROI140%)
    - G2/G1: 除外（サンプル不足）
    共通: EV 2.0-20
    """
    if odds is None or odds == 0: return False
    if not (2.0 <= ev7 <= 20.0): return False

    if grade == '新馬': return False
    if grade == '未勝利': return heads >= 10 and 10 <= odds <= 20
    if grade == '1勝':   return heads >= 10 and 3 <= odds <= 8
    if grade == '2勝':   return heads >= 10 and 15 <= odds <= 30
    if grade == '3勝':   return heads >= 8  and 5 <= odds <= 20
    if grade == 'G3':    return heads >= 10 and gap >= 3 and 8 <= odds <= 20
    return False


# ── 施策1+3: ▲廃止・買い目再配分の払戻計算 ─────────────────
def get_payout_v3(conn, date, venue, race_num, honmei_name, ni_name):
    """
    施策1+3の払戻:
      単勝◎  400円 → tansho_payout × 4
      馬連◎○ 300円 → umaren_payout × 3 (100円あたり×3)
      ワイド◎○ 300円 → wide_payout × 3 (100円あたり×3)
    ▲関連は買わない
    """
    div = _get_div_cached(conn, date, venue, race_num)
    if not div:
        return 0, 0, 0, []

    h_fin = _get_finish(conn, date, venue, race_num, honmei_name)
    if h_fin is None:
        return 0, 0, 0, []

    # 単勝
    pay_t = int((div.get("tansho_payout") or 0) * 4) if h_fin == 1.0 else 0

    # 馬連・ワイド
    pay_u = 0; pay_w = 0
    if ni_name:
        a_fin = _get_finish(conn, date, venue, race_num, ni_name)
        if a_fin is not None:
            fin_pair = {h_fin, a_fin}
            # 馬連: 300円(100円あたり×3)
            if fin_pair == {1.0, 2.0}:
                pay_u = int((div.get('umaren_payout') or 0) * 3)
            # ワイド: 300円(100円あたり×3)
            if fin_pair == {1.0, 2.0}:
                pay_w = int((div.get('wide1_payout') or 0) * 3)
            elif fin_pair == {1.0, 3.0}:
                pay_w = int((div.get('wide2_payout') or 0) * 3)
            elif fin_pair == {2.0, 3.0}:
                pay_w = int((div.get('wide3_payout') or 0) * 3)

    hits = []
    if pay_t: hits.append(f'単{pay_t}')
    if pay_u: hits.append(f'馬連○{pay_u}')
    if pay_w: hits.append(f'W○{pay_w}')

    return pay_t, pay_u, pay_w, hits


# ── 1ヶ月実行 ─────────────────────────────────────────────
def run_month_v3(conn, sc_conn, year, month):
    d_from = f'{year}-{month:02d}-01'
    d_to   = f'{year}-{month:02d}-31'
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
        ev_ok = is_buy_v3(gr, len(result), gap, honmei_odds or 0, ev7)

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


# ── 年単位実行 ─────────────────────────────────────────────
def run_year_v3(year, conn, sc_conn):
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

    import scoring as _sc
    _sc._bloodline_score_cache.clear()
    _sc._gcbb_cache.clear(); _sc._gcbb_loaded = False
    _sc._tbb_cache.clear(); _sc._week_cache.clear()

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
    return all_races, bet_records


# ── 集計 ─────────────────────────────────────────────────
def summarize_v3(year, all_races, bet_records):
    n = len(bet_records)
    ret = sum(r['ret'] for r in bet_records)
    roi = ret / (n * 1000) * 100 if n else 0
    prof = int(ret - n * 1000)

    wins_h = sum(1 for r in all_races if r['actual_win'])
    top3_h = sum(1 for r in all_races if r['winner_rank'] and r['winner_rank'] <= 3)
    tot_r = len(all_races)

    monthly = defaultdict(lambda: {'n': 0, 'ret': 0.0})
    for r in bet_records:
        ym = r['date'][:7]; monthly[ym]['n'] += 1; monthly[ym]['ret'] += r['ret']
    black = sum(1 for m in monthly.values() if m['ret'] > m['n'] * 1000)

    gd = defaultdict(lambda: {'n': 0, 'ret': 0.0})
    for r in bet_records:
        gd[r['grade']]['n'] += 1; gd[r['grade']]['ret'] += r['ret']

    return {
        'year': year, 'total_races': tot_r, 'n_bet': n,
        'profit': prof, 'roi': round(roi, 1),
        'honmei_win_rate':  round(wins_h / tot_r * 100, 1) if tot_r else 0,
        'honmei_top3_rate': round(top3_h / tot_r * 100, 1) if tot_r else 0,
        'black_months': black, 'total_months': len(monthly),
        'grade_detail': {
            g: {'n': v['n'], 'roi': round(v['ret']/v['n']/10, 1),
                'profit': int(v['ret'] - v['n']*1000)}
            for g, v in gd.items()
        },
        'monthly_detail': {
            ym: {'n': v['n'], 'roi': round(v['ret']/v['n']/10, 1),
                 'profit': int(v['ret'] - v['n']*1000)}
            for ym, v in sorted(monthly.items())
        },
    }


# ── メイン ─────────────────────────────────────────────────
def run_single_year(year):
    """1年分を実行してJSON保存（subprocess用エントリポイント）"""
    import importlib; importlib.reload(scoring)

    DB_PATH = 'keiba.db'
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    sc_conn = get_conn(DB_PATH)

    print(f'\n{"="*55}')
    print(f'  backtest_v3 [4施策版]  {year}年')
    print(f'{"="*55}')

    t_start = time.time()
    all_races, bet_records = run_year_v3(year, conn, sc_conn)
    elapsed = time.time() - t_start

    s = summarize_v3(year, all_races, bet_records)
    s['elapsed_sec'] = round(elapsed, 1)

    fname = f'btv3_{year}.json'
    with open(fname, 'w', encoding='utf-8') as f:
        json.dump({'summary': s, 'bet_records': bet_records}, f,
                  ensure_ascii=False, default=str)

    print(f'\n  -- {year}年 結果 (4施策版) --')
    print(f'  買い: {s["n_bet"]}R / 全{s["total_races"]}R  ({elapsed:.0f}s)')
    print(f'  損益: {s["profit"]:+,}円   ROI: {s["roi"]}%')
    print(f'  グレード別:')
    for g in ['未勝利','1勝','2勝','3勝','G3']:
        if g in s['grade_detail']:
            v = s['grade_detail'][g]
            print(f'    {g}: {v["n"]}R  ROI={v["roi"]}%  損益{v["profit"]:+,}円')
    print(f'  -> {fname} 保存済み')

    conn.close(); sc_conn.close()
    return s


if __name__ == '__main__':
    if '--year' in sys.argv:
        # 単年実行モード（並列子プロセス用）
        idx = sys.argv.index('--year')
        year = int(sys.argv[idx + 1])
        run_single_year(year)
    else:
        # 全年逐次実行モード
        years = [int(a) for a in sys.argv[1:]] if len(sys.argv) > 1 else list(range(2020, 2027))

        results_all = {}
        for year in years:
            s = run_single_year(year)
            results_all[year] = s

        print(f'\n{"="*55}')
        print(f'  全年サマリー [4施策版]')
        print(f'{"="*55}')
        total_bet = 0; total_ret = 0
        for y in sorted(results_all):
            s = results_all[y]
            total_bet += s['n_bet']
            total_ret += s['n_bet'] * 1000 + s['profit']
            print(f'  {y}: {s["n_bet"]}R  ROI={s["roi"]}%  損益={s["profit"]:+,}円')
        if total_bet:
            total_roi = total_ret / (total_bet * 1000) * 100
            total_profit = int(total_ret - total_bet * 1000)
            print(f'  合計: {total_bet}R  ROI={total_roi:.1f}%  損益={total_profit:+,}円')
