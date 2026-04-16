"""
ノリシコ競馬AI / backtest_full.py
2021〜2025年バックテスト（ルール文書 norishiko_rules_public.md 準拠）

買いレース判定:
  新馬・未勝利・1勝・2勝: heads>=10 and EV>=2.0
  3勝クラス: heads>=8 and gap>=6 and 6<=odds<=15
  重賞(G1/G2/G3): heads>=10 and gap>=3 and 4<=odds<=10

買い目: 単勝◎400円 + 馬連◎-○▲各100円 + ワイド◎-○▲各200円 = 1,000円/R
"""

import sqlite3, json, time, sys, shutil, numpy as np
from collections import defaultdict
from pathlib import Path

DB_PATH = 'keiba.db'
sys.path.insert(0, '.')

for f in ['gate_style_bias.json', 'gate_cond_blood_stats.json']:
    if not Path(f).exists():
        src = Path(f'/mnt/user-data/uploads/{f}')
        if src.exists():
            shutil.copy(str(src), f)

import scoring as sc
from scoring import (get_conn, score_past_performance, score_course_fitness,
    score_jockey_trainer, score_rotation, score_training_actual, score_bloodline,
    score_gate_style, get_weights, calc_pace_context, _infer_running_style,
    calc_course_blood_bonus, calc_gate_cond_blood_bonus, calc_track_bias_bonus,
    _past_runs_cache, _course_runs_cache, _running_style_cache,
    _jockey_cache, _trainer_cache, _combo_cache, _ace_cache,
    _avg_time_cache, _last3f_cache, _training_actual_cache,
    _bloodline_score_cache, _week_cache,
    prefetch_training_lap1_stats, _training_lap1_stats,
    _vsb_cache, _vsb_loaded,
    _vdsb_cache, _vdsb_loaded)
from backtest_2026 import prefetch_month, clear_caches, score_one_race


def grade_full(n):
    s = str(n)
    for g in ['G1','G2','G3']:
        if g in s: return g
    if '(L)' in s or '（L）' in s: return '3勝'
    if '3勝' in s or '３勝' in s or '1600万' in s: return '3勝'
    if '2勝' in s or '２勝' in s or '1000万' in s: return '2勝'
    if '1勝' in s or '１勝' in s or '500万' in s:  return '1勝'
    if '新馬' in s: return '新馬'
    if '未勝利' in s: return '未勝利'
    return '3勝'


def is_buy_race(grade, heads, ev, gap, odds):
    if grade in ['新馬','未勝利','1勝','2勝']:
        return heads >= 10 and ev >= 2.0
    if grade == '3勝':
        return heads >= 8 and gap >= 6 and 6 <= (odds or 0) <= 15
    if grade in ['G1','G2','G3']:
        return heads >= 10 and gap >= 3 and 4 <= (odds or 0) <= 10
    return False


def calc_ev7(scores, odds_arr):
    arr = np.array(scores, dtype=float)
    exp_s = np.exp((arr - arr.mean()) / 7)
    probs = exp_s / exp_s.sum()
    evs = [p * (o or 0) for p, o in zip(probs, odds_arr)]
    return probs, evs


def prefetch_score_caches(conn, cutoff_date=None):
    '''avg_time / avg_last3f / track_bias_bonus を一括プリフェッチ
       cutoff_date: 指定時はその日付より前のデータのみ使用（リーク防止）'''
    import scoring as _sc2
    _sc2._avg_time_cache.clear()
    _sc2._last3f_cache.clear()

    date_filter = "AND date < ?" if cutoff_date else ""
    params_time = [cutoff_date] if cutoff_date else []

    # avg_time: surface×distance×track_cond
    rows = conn.execute(f'''
        SELECT surface, distance, track_cond,
               AVG(time_sec) as avg_t, MIN(time_sec) as best_t
        FROM results
        WHERE time_sec IS NOT NULL AND finish=1 AND time_sec > 0
        {date_filter}
        GROUP BY surface, distance, track_cond
    ''', params_time).fetchall()
    for r in rows:
        _sc2._avg_time_cache[(r['surface'], r['distance'], r['track_cond'])] = (r['avg_t'], r['best_t'])

    # avg_last3f: venue×distance×surface
    rows2 = conn.execute(f'''
        SELECT venue, distance, surface, AVG(last3f) as avg_l3f
        FROM results
        WHERE last3f IS NOT NULL AND last3f > 0
        {date_filter}
        GROUP BY venue, distance, surface
    ''', params_time).fetchall()
    for r in rows2:
        _sc2._last3f_cache[('__all__', r['venue'], r['distance'], r['surface'])] = r['avg_l3f']

    # track_bias_bonus: 全件をメモリにロード
    _sc2._tbb_cache.clear()
    tbb_rows = conn.execute('SELECT venue,surface,phase,gate_cat,style_cat,bonus FROM track_bias_bonus').fetchall()
    for r in tbb_rows:
        _sc2._tbb_cache[(r['venue'],r['surface'],r['phase'],r['gate_cat'],r['style_cat'])] = r['bonus']

    # gate_cond_blood_bonus: 全件再ロード
    _sc2._gcbb_cache.clear(); _sc2._gcbb_loaded = False
    _sc2._load_gcbb_all(conn)

    # bloodline_stats: 全件をメモリにプリフェッチ（個別クエリ排除）
    _sc2._bloodline_score_cache.clear()
    bl_rows = conn.execute('SELECT col_type, name, surface, dist_bucket, score FROM bloodline_stats').fetchall()
    for r in bl_rows:
        _sc2._bloodline_score_cache[(r['col_type'], r['name'], r['surface'], r['dist_bucket'])] = r['score']

    # week_cache: (venue,date)→週番号 を一括構築（_get_opening_week のDBクエリを排除）
    _sc2._week_cache.clear()
    if True:
        from datetime import datetime
        from collections import defaultdict
        wk_filter = f"AND date < '{cutoff_date}'" if cutoff_date else ""
        all_vd = conn.execute(f"""
            SELECT DISTINCT venue,date FROM results WHERE finish<90 {wk_filter} ORDER BY venue,date
        """).fetchall()
        venue_dates = defaultdict(list)
        for r in all_vd: venue_dates[r['venue']].append(r['date'])
        for venue, dates in venue_dates.items():
            dates = sorted(set(dates))
            wn = 1; prev_d = None
            for d in dates:
                if prev_d:
                    gap = (datetime.strptime(d,'%Y-%m-%d') -
                           datetime.strptime(prev_d,'%Y-%m-%d')).days
                    if gap > 21: wn = 1
                    elif gap >= 6: wn += 1
                _sc2._week_cache[(venue, d)] = wn
                prev_d = d

    # training lap1統計: source別のmean/stdをプリフェッチ（偏差値計算用）
    prefetch_training_lap1_stats(conn, cutoff_date or '2099-01-01')

    # venue_sire_bonus / gate_sire_bonus / track_cond_sire_bonus: リーク防止で再構築
    import scoring as _sc3
    _cutoff = cutoff_date or '2099-01-01'

    _sc3._vsb_cache.clear(); _sc3._vsb_loaded = False
    from build_venue_sire_bonus import build_venue_sire_bonus
    build_venue_sire_bonus(conn, cutoff_date=_cutoff)
    _sc3._load_vsb_all(conn)

    _sc3._vdsb_cache.clear(); _sc3._vdsb_loaded = False
    from build_venue_damsire_bonus import build_venue_damsire_bonus
    build_venue_damsire_bonus(conn, cutoff_date=_cutoff)
    _sc3._load_vdsb_all(conn)

    # cushion_sire_bonus も同様にリーク防止で再構築
    _sc3._csb_cache.clear(); _sc3._csb_loaded = False
    try:
        from build_cushion_sire_bonus import build_cushion_sire_bonus
        build_cushion_sire_bonus(conn, cutoff_date=_cutoff)
        _sc3._load_csb_all(conn)
    except Exception as e:
        print(f"  cushion_sire_bonus rebuild skipped: {e}")

    # nicks_bonus もリーク防止で再構築
    _sc3._nicks_cache.clear(); _sc3._nicks_loaded = False
    try:
        from build_nicks_bonus import build_nicks_bonus
        build_nicks_bonus(conn, cutoff_date=_cutoff)
        _sc3._load_nicks_all(conn)
    except Exception as e:
        print(f"  nicks_bonus rebuild skipped: {e}")

    # daily_track_bias もリーク防止で再構築 (cutoff前の結果のみで判定+分布計算)
    try:
        _sc3._dtb_cache.clear(); _sc3._dtb_loaded = False
        from build_daily_track_bias import build_daily_track_bias
        build_daily_track_bias(conn, cutoff_date=_cutoff)
        _sc3._load_dtb_all(conn)
    except Exception as e:
        print(f"  daily_track_bias rebuild skipped: {e}")



def prefetch_jt(conn, year, month):
    d_from=f'{year}-{month:02d}-01'; d_to=f'{year}-{month:02d}-31'
    d_2y=f'{year-2}-{month:02d}-01'; d_1y=f'{year-1}-{month:02d}-01'
    yh=str(year)+('H1' if month<=6 else 'H2')
    combos=conn.execute('SELECT DISTINCT jockey,trainer,surface,distance FROM results WHERE date BETWEEN ? AND ? AND finish<90',(d_from,d_to)).fetchall()
    jockeys=list(set((r['jockey'],r['surface'],r['distance']//400) for r in combos))
    trainers=list(set(r['trainer'] for r in combos))
    pairs=list(set((r['jockey'],r['trainer']) for r in combos))
    uncached_j=[(j,s,d) for j,s,d in jockeys if (j,s,d,yh) not in _jockey_cache]
    if uncached_j:
        jnames=list(set(j for j,s,d in uncached_j)); ph=','.join(['?']*len(jnames))
        rows=conn.execute(f'SELECT jockey,surface,CAST(distance/400 AS INTEGER) as db,SUM(CASE WHEN finish=1 THEN 1.0 ELSE 0 END) as wins,SUM(CASE WHEN finish<=3 THEN 1.0 ELSE 0 END) as top3,COUNT(*) as n FROM results WHERE jockey IN ({ph}) AND date BETWEEN ? AND ? AND finish<90 GROUP BY jockey,surface,CAST(distance/400 AS INTEGER)',jnames+[d_2y,d_from]).fetchall()
        j_map={(r['jockey'],r['surface'],r['db']):(r['wins'],r['top3'],r['n']) for r in rows}
        for j,s,d in uncached_j:
            jw,jt,jn=j_map.get((j,s,d),(0,0,0))
            js,jr=(min(100,jw/jn*300+jt/jn*50),jw/jn) if jn>=10 else (60.0,0.10)
            _jockey_cache[(j,s,d,yh)]=(js,jr)
    uncached_t=[t for t in trainers if (t,yh) not in _trainer_cache]
    if uncached_t:
        ph2=','.join(['?']*len(uncached_t))
        rows2=conn.execute(f'SELECT trainer,SUM(CASE WHEN finish=1 THEN 1.0 ELSE 0 END) as wins,COUNT(*) as n FROM results WHERE trainer IN ({ph2}) AND date BETWEEN ? AND ? AND finish<90 GROUP BY trainer',uncached_t+[d_1y,d_from]).fetchall()
        t_map={r['trainer']:(r['wins'],r['n']) for r in rows2}
        for t in uncached_t:
            tw,tn=t_map.get(t,(0,0)); _trainer_cache[(t,yh)]=min(100,tw/tn*400) if tn>=10 else 60.0
    uncached_c=[(j,t) for j,t in pairs if (j,t,yh) not in _combo_cache]
    if uncached_c:
        all_t=list(set(t for _,t in uncached_c)); ph3=','.join(['?']*len(all_t))
        rows3=conn.execute(f'SELECT jockey,trainer,SUM(CASE WHEN finish=1 THEN 1.0 ELSE 0 END) as wins,COUNT(*) as n FROM results WHERE trainer IN ({ph3}) AND date BETWEEN ? AND ? AND finish<90 GROUP BY jockey,trainer',all_t+[d_2y,d_from]).fetchall()
        c_map={(r['jockey'],r['trainer']):(r['wins'],r['n']) for r in rows3}
        for jockey,trainer in uncached_c:
            c_key=(jockey,trainer,yh); jw,jn=c_map.get((jockey,trainer),(0,0))
            j_key0=next(((j,s,d,yh) for j,s,d in jockeys if j==jockey),None)
            jr=_jockey_cache.get(j_key0,(60.0,0.10))[1] if j_key0 else 0.10
            _combo_cache[c_key]=max(-5.0,min(5.0,(jw/jn-jr)*60)) if jn>=10 else 0.0
            t_jw={r['jockey']:r['wins'] for r in rows3 if r['trainer']==trainer}; tw2=sum(t_jw.values())
            ap=t_jw.get(jockey,0)/tw2*100 if tw2>=5 else 0.0
            ab=3.0 if ap>=40 else 2.0 if ap>=25 else 1.0 if ap>=15 else 0.5 if ap>=8 else 0.0
            _ace_cache[c_key]=(ab,ap)


def get_payout(conn, date, venue, race_num, uma1, uma2, bet_type):
    d = conn.execute('SELECT * FROM dividends WHERE date=? AND venue=? AND race_num=?',(date,venue,race_num)).fetchone()
    if not d: return 0
    d = dict(d)
    if bet_type == 'tansho':
        return (d.get('tansho_payout') or 0)*10 if d.get('tansho_umaban')==uma1 else 0
    if bet_type == 'umaren':
        pair={uma1,uma2}
        for u1k,u2k,pk in [('umaren_uma1','umaren_uma2','umaren_payout'),('umaren2_uma1','umaren_uma2','umaren2_payout')]:
            u1,u2,pay=d.get(u1k),d.get(u2k),d.get(pk)
            if u1 and u2 and pay and {u1,u2}==pair: return pay*10
        return 0
    if bet_type == 'wide':
        pair={uma1,uma2}
        for u1k,u2k,pk in [('wide1_uma1','wide1_uma2','wide1_payout'),('wide2_uma1','wide2_uma2','wide2_payout'),('wide3_uma1','wide3_uma2','wide3_payout')]:
            u1,u2,pay=d.get(u1k),d.get(u2k),d.get(pk)
            if u1 and u2 and pay and {u1,u2}==pair: return pay*10
        return 0
    return 0


def run_year(year, conn, sc_conn):
    """1年分のバックテストを実行して結果リストを返す"""
    prefetch_score_caches(conn)  # avg_time/avg_last3f 全件プリフェッチ
    months = range(1, 13)
    bet_records = []
    all_races = []

    for month in months:
        d_from = f'{year}-{month:02d}-01'
        d_to   = f'{year}-{month:02d}-31'

        if month == 1: clear_caches(full=True)
        else: clear_caches(full=False)

        prefetch_month(conn, year, month)
        prefetch_jt(conn, year, month)

        races = conn.execute(
            'SELECT DISTINCT date,venue,race_num FROM results WHERE date BETWEEN ? AND ? AND finish<90 ORDER BY date,venue,race_num',
            (d_from, d_to)
        ).fetchall()

        for race in races:
            rows = [dict(r) for r in conn.execute(
                'SELECT * FROM results WHERE date=? AND venue=? AND race_num=? AND finish<90 ORDER BY horse_num',
                (race['date'], race['venue'], race['race_num'])
            ).fetchall()]
            if len(rows) < 3: continue
            rname = rows[0].get('race_name', '')
            if '障害' in str(rname): continue

            gr = grade_full(rname)
            heads = len(rows)

            try:
                result, meta = score_one_race(rows, sc_conn)
            except: continue
            if not result or len(result) < 2: continue

            honmei = result[0]
            ni     = result[1]
            san    = result[2] if len(result) > 2 else None

            scores   = [h['total_score'] for h in result]
            odds_arr = [h.get('odds') or 0 for h in result]
            probs, evs = calc_ev7(scores, odds_arr)

            honmei_ev   = evs[0]
            honmei_odds = honmei.get('odds')
            gap         = meta['standout_gap']
            winner_rank = next((h['rank'] for h in result if h['finish'] == 1), None)

            rec = {
                'date': race['date'], 'venue': race['venue'],
                'race_num': race['race_num'], 'grade': gr,
                'heads': heads, 'gap': gap,
                'honmei_name': honmei['horse_name'],
                'honmei_num':  honmei['horse_num'],
                'honmei_odds': honmei_odds,
                'honmei_ev':   round(honmei_ev, 3),
                'honmei_finish': honmei['finish'],
                'winner_rank': winner_rank,
                'actual_win':  (honmei['finish'] == 1),
            }
            all_races.append(rec)

            buy = is_buy_race(gr, heads, honmei_ev, gap, honmei_odds)
            if not buy: continue

            d  = race['date']; v = race['venue']; rn = race['race_num']
            hn = honmei['horse_num']
            nn = ni['horse_num']
            sn = san['horse_num'] if san else None

            pay_t  = get_payout(conn, d, v, rn, hn, None, 'tansho')
            pay_u1 = get_payout(conn, d, v, rn, hn, nn,   'umaren')
            pay_u2 = get_payout(conn, d, v, rn, hn, sn,   'umaren') if sn else 0
            pay_w1 = get_payout(conn, d, v, rn, hn, nn,   'wide')
            pay_w2 = get_payout(conn, d, v, rn, hn, sn,   'wide') if sn else 0

            ret = (pay_t/100*400 + pay_u1/100*100 + pay_u2/100*100
                   + pay_w1/100*200 + pay_w2/100*200)

            hits = []
            if pay_t:  hits.append(f'単{pay_t/100*400:.0f}')
            if pay_u1: hits.append(f'馬連○{pay_u1/100*100:.0f}')
            if pay_u2: hits.append(f'馬連▲{pay_u2/100*100:.0f}')
            if pay_w1: hits.append(f'W○{pay_w1/100*200:.0f}')
            if pay_w2: hits.append(f'W▲{pay_w2/100*200:.0f}')

            bet_records.append({
                **rec,
                'ret':    ret,
                'profit': ret - 1000,
                'hits':   ' + '.join(hits) if hits else '全外れ',
            })

        sys.stdout.write(f'\r  {year}年{month:02d}月 {len(bet_records)}R賭け済  ')
        sys.stdout.flush()

    print()
    return all_races, bet_records


def summarize(all_races, bet_records, year):
    total_r = len(all_races)
    n_bet   = len(bet_records)
    total_bet = n_bet * 1000
    total_ret = sum(r['ret'] for r in bet_records)
    roi = total_ret / total_bet * 100 if total_bet > 0 else 0
    profit = total_ret - total_bet

    wins_h = sum(1 for r in all_races if r['actual_win'])
    top3_h = sum(1 for r in all_races if r['winner_rank'] and r['winner_rank'] <= 3)

    # グレード別
    grade_stats = defaultdict(lambda: {'n':0,'ret':0})
    for r in bet_records:
        g = r['grade']
        grade_stats[g]['n']   += 1
        grade_stats[g]['ret'] += r['ret']

    # 月別
    monthly = defaultdict(lambda: {'n':0,'ret':0})
    for r in bet_records:
        ym = r['date'][:7]
        monthly[ym]['n']   += 1
        monthly[ym]['ret'] += r['ret']

    black_months = sum(1 for m in monthly.values() if m['ret'] > m['n']*1000)
    total_months = len(monthly)

    return {
        'year': year,
        'total_races': total_r,
        'n_bet': n_bet,
        'total_bet': total_bet,
        'total_ret': int(total_ret),
        'profit': int(profit),
        'roi': round(roi, 1),
        'honmei_win_rate': round(wins_h/total_r*100, 1) if total_r else 0,
        'honmei_top3_rate': round(top3_h/total_r*100, 1) if total_r else 0,
        'black_months': black_months,
        'total_months': total_months,
        'grade_stats': {g: {'n':v['n'],'roi':round(v['ret']/v['n']/10,1)} for g,v in grade_stats.items()},
        'monthly': {ym: {'n':v['n'],'profit':int(v['ret']-v['n']*1000),'roi':round(v['ret']/v['n']/10,1)} for ym,v in sorted(monthly.items())},
    }


if __name__ == '__main__':
    target_year = int(sys.argv[1]) if len(sys.argv) > 1 else 2021

    print(f'\n{"="*60}')
    print(f'  バックテスト {target_year}年')
    print(f'{"="*60}')

    conn    = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    sc_conn = get_conn(DB_PATH)

    t0 = time.time()
    all_races, bet_records = run_year(target_year, conn, sc_conn)
    elapsed = time.time() - t0

    result = summarize(all_races, bet_records, target_year)
    result['elapsed_sec'] = round(elapsed, 1)

    out_path = f'bt_{target_year}.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    print(f'  {target_year}年完了: {result["n_bet"]}R賭け ROI={result["roi"]}% 損益{result["profit"]:+,}円 ({elapsed:.0f}s)')
    print(f'  → {out_path} 保存済み')

    conn.close(); sc_conn.close()
