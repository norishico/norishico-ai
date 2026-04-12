"""
backtest_v2.py — ノリシコ競馬AI バックテスト（案A完全版）

■ 仕様
  スコアリング : v2.0（8要素、score_training_actual使用）
  EV計算      : scale=12 + is_ev_race()（backtest_pnl.py準拠）
                 EV% = (win_prob×odds-1)×100 >= 5%
                 gap >= 8, odds 3〜30倍
  買い目      : 単勝◎400円 + 馬連◎-○▲各100円 + ワイド◎-○▲各200円 = 1,000円/R
  払戻        : dividendsテーブル（現行スキーマ）
"""

import sys, time, json, sqlite3, numpy as np
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, '.')

import scoring
from scoring import (get_conn, is_ev_race,
    _avg_time_cache, _last3f_cache, _tbb_cache, _gcbb_loaded,
    _jockey_cache, _trainer_cache, _combo_cache, _ace_cache,
    _past_runs_cache, _course_runs_cache, _running_style_cache,
    _training_actual_cache, _bloodline_score_cache, _week_cache)
from backtest_2026 import prefetch_month, clear_caches
from backtest_full  import prefetch_jt, prefetch_score_caches, score_one_race, grade_full


# ── scale=12 win_prob ─────────────────────────────────────
def calc_win_prob_s12(all_scores):
    arr = np.array(all_scores, dtype=float)
    exp_s = np.exp((arr - arr.mean()) / 12)
    probs = exp_s / exp_s.sum()
    return float(probs[0])


def calc_ev_scale7(all_scores, honmei_odds):
    """実運用(rescore_month.py)と同じ scale=7 で EV を算出"""
    arr = np.array(all_scores, dtype=float)
    exp_s = np.exp((arr - arr.mean()) / 7)
    probs = exp_s / exp_s.sum()
    return float(probs[0]) * honmei_odds  # 2.0以上が買いライン


def is_buy_official(grade, heads, gap, odds, ev7):
    """買い条件（未勝利+1勝集中 / EV2.0〜20 / odds上限30倍）"""
    if odds is None or odds == 0: return False
    if not (2.0 <= ev7 <= 20.0): return False
    if odds > 30: return False
    if grade in ['未勝利', '1勝']:
        return heads >= 10
    return False


# ── 配当取得（着順ベース・馬番ズレ対応版）────────────────
# results.horse_num はスコアランク連番でズレているため馬番照合に使えない
# dividends の wide馬番のみ実際の馬番が正しく格納されている
# → 単勝・馬連は finish で判定、ワイドは wide馬番を逆引きして特定

def _get_finish(conn, date, venue, race_num, horse_name):
    """horse_nameからfinishを返す（trim対応）"""
    r = conn.execute(
        'SELECT finish FROM results WHERE date=? AND venue=? AND race_num=? AND trim(horse_name)=?',
        (date, venue, race_num, horse_name.strip())
    ).fetchone()
    return float(r['finish']) if r else None


def _real_num_from_wide(div):
    """dividendsのwideペアから3着内実馬番集合を返す"""
    nums = set()
    for i in [1, 2, 3]:
        u1 = div.get(f'wide{i}_uma1')
        u2 = div.get(f'wide{i}_uma2')
        if u1 is not None: nums.add(int(u1))
        if u2 is not None: nums.add(int(u2))
    return nums  # 通常3頭分


def _get_real_num(conn, date, venue, race_num, horse_name, div):
    """
    horse_nameの実馬番をdividendsから逆引きする。

    根拠:
    - sanrentanの uma1/2/3 は「馬番昇順」格納で着順ではないため使わない
    - umatan の uma1=1着馬番, uma2=2着馬番（着順順、全レースでwideと整合確認済み）
    - 3着馬番 = wide3頭集合 - umatanペア の残り1頭
    """
    fin = _get_finish(conn, date, venue, race_num, horse_name)
    if fin is None or fin > 3:
        return None

    ut1 = div.get('umatan_uma1')
    ut2 = div.get('umatan_uma2')
    if ut1 is None or ut2 is None:
        return None

    real_nums = _real_num_from_wide(div)
    if len(real_nums) != 3:
        return None

    ut_set = {int(ut1), int(ut2)}
    # umatanがwideと整合しない場合はスキップ
    if not ut_set.issubset(real_nums):
        return None

    third_set = real_nums - ut_set  # 3着馬番（1頭のはず）

    if fin == 1.0:
        return int(ut1)
    if fin == 2.0:
        return int(ut2)
    if fin == 3.0:
        return int(third_set.pop()) if len(third_set) == 1 else None

    return None


_div_cache = {}

def _get_div_cached(conn, date, venue, race_num):
    """dividendsを1レース1回だけ取得（モジュールレベルキャッシュ）"""
    key = (date, venue, race_num)
    if key not in _div_cache:
        r = conn.execute(
            'SELECT * FROM dividends WHERE date=? AND venue=? AND race_num=?',
            key
        ).fetchone()
        _div_cache[key] = dict(r) if r else None
    return _div_cache[key]


def get_payout_v2(conn, date, venue, race_num, honmei_name, aite_name, bet_type):
    """
    着順ベースの払戻計算

      tansho_payout → 単勝オッズ値（×400で払戻円）
      umaren_payout → 馬連払戻（100円あたり）
      wide1_payout  → ワイド1-2着払戻（100円あたり）
      wide2_payout  → ワイド1-3着払戻（100円あたり）
      wide3_payout  → ワイド2-3着払戻（100円あたり）

    単勝 : finish==1 → tansho_payout × 400
    馬連 : {◎,aite}finish=={1,2} → umaren_payout × 1
    ワイド: {◎,aite}finish==
              {1,2} → wide1_payout × 2
              {1,3} → wide2_payout × 2
              {2,3} → wide3_payout × 2
    """
    div = _get_div_cached(conn, date, venue, race_num)
    if not div:
        return 0

    h_fin = _get_finish(conn, date, venue, race_num, honmei_name)
    if h_fin is None:
        return 0

    # ── 単勝 ──────────────────────────────────────────────
    if bet_type == 'tansho':
        return int((div.get("tansho_payout") or 0) * 4) if h_fin == 1.0 else 0

    if aite_name is None:
        return 0
    a_fin = _get_finish(conn, date, venue, race_num, aite_name)
    if a_fin is None:
        return 0

    fin_pair = {h_fin, a_fin}

    # ── 馬連 ──────────────────────────────────────────────
    if bet_type == 'umaren':
        if fin_pair != {1.0, 2.0}:
            return 0
        return int(div.get('umaren_payout') or 0)

    # ── ワイド ────────────────────────────────────────────
    if bet_type == 'wide':
        if fin_pair == {1.0, 2.0}:
            return int(div.get('wide1_payout') or 0) * 2
        if fin_pair == {1.0, 3.0}:
            return int(div.get('wide2_payout') or 0) * 2
        if fin_pair == {2.0, 3.0}:
            return int(div.get('wide3_payout') or 0) * 2
        return 0

    return 0

# 後方互換エイリアス（旧コードへの影響を最小化）
def get_payout(conn, date, venue, race_num, uma1, uma2, bet_type):
    """旧インターフェース（馬番ズレあり）→ 新関数へブリッジ不可のため保持"""
    # このパスは run_month() から呼ばれなくなる
    return 0


# ── 1ヶ月実行 ─────────────────────────────────────────────
def run_month(conn, sc_conn, year, month):
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
        san    = result[2] if len(result) > 2 else None

        # scale=12 win_prob → is_ev_race
        scores      = [h['total_score'] for h in result]
        win_prob    = calc_win_prob_s12(scores)
        gap         = meta['standout_gap']
        honmei_odds = honmei.get('odds')

        # 実運用ルール（norishiko_rules_public.md §6）で買い判定
        scores_list = [h['total_score'] for h in result]
        ev7    = calc_ev_scale7(scores_list, honmei_odds or 0)
        ev_ok  = is_buy_official(gr, len(result), gap, honmei_odds or 0, ev7)

        # EV計算（表示用: scale=12）
        honmei_ev = win_prob * (honmei_odds or 0)

        winner_rank = next((h['rank'] for h in result if h['finish'] == 1), None)
        rec = {
            'date': race['date'], 'venue': race['venue'],
            'race_num': race['race_num'], 'grade': gr,
            'heads': len(rows), 'gap': round(gap, 1),
            'honmei_name':  honmei['horse_name'],
            'honmei_num':   honmei['horse_num'],
            'honmei_odds':  honmei_odds,
            'honmei_ev':    round(honmei_ev, 3),
            'win_prob_pct': round(win_prob * 100, 1),
            'honmei_finish': honmei['finish'],
            'ni_name':      ni['horse_name'],
            'ni_finish':    ni['finish'],
            'san_name':     san['horse_name'] if san else None,
            'san_finish':   san['finish'] if san else None,
            'winner_rank':  winner_rank,
            'actual_win':   (honmei['finish'] == 1),
            'ev_ok':        ev_ok,
        }
        all_races.append(rec)
        if not ev_ok: continue

        d  = race['date']; v = race['venue']; rn = race['race_num']
        hn_name = honmei['horse_name']
        ni_name = ni['horse_name']
        sn_name = san['horse_name'] if san else None

        pay_t  = get_payout_v2(conn, d, v, rn, hn_name, None,    'tansho')
        pay_u1 = get_payout_v2(conn, d, v, rn, hn_name, ni_name, 'umaren')
        pay_u2 = get_payout_v2(conn, d, v, rn, hn_name, sn_name, 'umaren') if sn_name else 0
        pay_w1 = get_payout_v2(conn, d, v, rn, hn_name, ni_name, 'wide')
        pay_w2 = get_payout_v2(conn, d, v, rn, hn_name, sn_name, 'wide') if sn_name else 0

        # 各get_payout_v2の戻り値:
        # tansho  → tansho_payout * 400           (直接払戻円)
        # umaren  → wide1_payout * 1 (100円あたり) → × 1 = 払戻円
        # wide    → payout * 2      (100円あたり×2) → 直接払戻円
        ret = pay_t + pay_u1 + pay_u2 + pay_w1 + pay_w2
        hits = []
        if pay_t:  hits.append(f'単{pay_t:.0f}')
        if pay_u1: hits.append(f'馬連○{pay_u1:.0f}')
        if pay_u2: hits.append(f'馬連▲{pay_u2:.0f}')
        if pay_w1: hits.append(f'W○{pay_w1:.0f}')
        if pay_w2: hits.append(f'W▲{pay_w2:.0f}')

        bets.append({**rec, 'ret': ret, 'profit': ret - 1000,
                     'hits': ' + '.join(hits) if hits else '全外れ'})

    return all_races, bets


# ── 年単位実行 ─────────────────────────────────────────────
def run_year(year, conn, sc_conn):
    all_races = []; bet_records = []
    t0 = time.time()
    _div_cache.clear()  # 年跨ぎでキャッシュリセット

    # 補助テーブルを年初cutoffで再構築（未来データリーク防止）
    from build_supplementary_tables import (
        build_bloodline_stats, build_gate_cond_blood_bonus, build_track_bias_bonus
    )
    cutoff = f'{year}-01-01'
    build_bloodline_stats(sc_conn, cutoff_date=cutoff)
    build_gate_cond_blood_bonus(sc_conn, cutoff_date=cutoff)
    build_track_bias_bonus(sc_conn, cutoff_date=cutoff)
    # scoring.pyのテーブルキャッシュもクリア
    import scoring as _sc
    _sc._bloodline_score_cache.clear()
    _sc._gcbb_cache.clear(); _sc._gcbb_loaded = False
    _sc._tbb_cache.clear(); _sc._week_cache.clear()

    for month in range(1, 13):
        if month == 1: clear_caches(full=True); prefetch_score_caches(sc_conn, cutoff_date=cutoff)
        else:          clear_caches(full=False)
        prefetch_month(conn, year, month)
        prefetch_jt(conn, year, month)

        ar, br = run_month(conn, sc_conn, year, month)
        all_races  += ar
        bet_records += br
        sys.stdout.write(f'\r  {year}/{month:02d} 全{len(ar)}R 買{len(br)}R 累{time.time()-t0:.0f}s  ')
        sys.stdout.flush()
    print()
    return all_races, bet_records


# ── 集計 ─────────────────────────────────────────────────
def summarize(year, all_races, bet_records):
    n   = len(bet_records)
    ret = sum(r['ret'] for r in bet_records)
    roi = ret / (n * 1000) * 100 if n else 0
    prof = int(ret - n * 1000)

    wins_h = sum(1 for r in all_races if r['actual_win'])
    top3_h = sum(1 for r in all_races if r['winner_rank'] and r['winner_rank'] <= 3)
    tot_r  = len(all_races)

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


if __name__ == '__main__':
    import importlib; importlib.reload(scoring)
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2021

    DB_PATH = 'keiba.db'
    conn    = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    sc_conn = get_conn(DB_PATH)

    print(f'\n{"="*55}')
    print(f'  backtest_v2  {year}年')
    print(f'  スコア: v2.0  EV: scale=12+is_ev_race  買目: ノリシコ1000円')
    print(f'{"="*55}')

    prefetch_score_caches(sc_conn)
    t_start = time.time()
    all_races, bet_records = run_year(year, conn, sc_conn)
    elapsed = time.time() - t_start

    s = summarize(year, all_races, bet_records)
    s['elapsed_sec'] = round(elapsed, 1)

    with open(f'btv2_{year}.json', 'w', encoding='utf-8') as f:
        json.dump({'summary': s, 'bet_records': bet_records}, f,
                  ensure_ascii=False, default=str)

    documented = {2021:{'r':866,'roi':241.8}, 2022:{'r':711,'roi':169.3},
                  2023:{'r':692,'roi':164.0}, 2024:{'r':715,'roi':167.9},
                  2025:{'r':715,'roi':216.1}, 2026:{'r':591,'roi':133.3}}
    doc = documented.get(year, {})

    print(f'\n  ── {year}年 結果 ──')
    print(f'  買い: {s["n_bet"]}R / 全{s["total_races"]}R  ({elapsed:.0f}s)')
    print(f'  損益: {s["profit"]:+,}円   ROI: {s["roi"]}%')
    if doc:
        print(f'  記載: {doc["r"]}R / {doc["roi"]}%  '
              f'(R差{s["n_bet"]-doc["r"]:+d}  ROI差{s["roi"]-doc["roi"]:+.1f}pt)')
    print(f'  ◎単勝率: {s["honmei_win_rate"]}%  ◎3着内率: {s["honmei_top3_rate"]}%  黒字月: {s["black_months"]}/{s["total_months"]}')
    print(f'  グレード別:')
    for g in ['新馬','未勝利','1勝','2勝','3勝','G3','G2','G1']:
        if g in s['grade_detail']:
            v = s['grade_detail'][g]
            print(f'    {g}: {v["n"]}R  ROI={v["roi"]}%  損益{v["profit"]:+,}円')
    print(f'  → btv2_{year}.json 保存済み')

    conn.close(); sc_conn.close()
