"""
backtest_v6.py — ゼロベース再設計版

各クラスの買い条件を年別安定性データから導出:
  新馬: 除外
  未勝利: 除外（安定的に黒字の条件なし）
  1勝: odds 3-5（5/7年黒字, ROI98%）
  2勝: odds 20-30（4/7年黒字, ROI102%）
  3勝: odds 5-20 & heads12+（5/7年黒字, ROI135%）
       OR odds 8-20 & gap>=8（6/7年黒字, ROI180%）
  G3: odds 3-16 & gap 3-8 & heads14+（5/7年黒字, ROI160%）
  G2/G1: 除外（サンプル不足）

  共通: EV 2.0-20
  買い目: 単勝◎400 + 馬連◎○300 + ワイド◎○300 = 1,000円
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


# 調教フィルタ用: サンデーサイレンス系種牡馬リスト
SUNDAY_SIRES = frozenset({
    'ディープインパクト','ハーツクライ','キズナ','ステイゴールド','オルフェーヴル',
    'ゴールドシップ','ドゥラメンテ','スクリーンヒーロー','モーリス','エピファネイア',
    'ジャスタウェイ','リアルスティール','サトノダイヤモンド','キタサンブラック',
    'ワールドプレミア','サトノクラウン','スワーヴリチャード','シルバーステート',
    'ブラックタイド','マカヒキ','ダイワメジャー','ネオユニヴァース',
})


def is_buy_v6(grade, heads, gap, odds, ev7, good_train=True, sire='', track_cond=''):
    """v6.4 通常買い条件 + 調教フィルタ + 不良馬場フィルタ

    Returns: ('normal', True/False) for 通常ゾーン
             ('challenge', True/False) for チャレンジゾーン
             (None, False) for 見送り
    """
    if track_cond in ('不', '不良'): return None, False  # 不良馬場は買わない
    if odds is None or odds == 0: return None, False
    if not (2.0 <= ev7 <= 20.0): return None, False

    if grade == '新馬': return None, False
    if grade == '未勝利': return None, False

    if grade == '1勝':
        if not good_train: return None, False
        if 3 <= odds <= 5: return 'normal', True
        if 5 < odds <= 6: return 'challenge', True   # ROI141%, 4/6年黒字
        return None, False

    if grade == '2勝':
        if 20 <= odds <= 30: return 'normal', True
        if 30 < odds <= 40: return 'challenge', True  # ROI206%, 3/7年黒字
        return None, False

    if grade == '3勝':
        if sire in SUNDAY_SIRES and not good_train:
            return None, False
        cond_a = (heads >= 12)
        cond_b = (gap >= 8)
        if not (cond_a or cond_b): return None, False
        if (5 <= odds <= 20 and cond_a) or (8 <= odds <= 20 and cond_b):
            return 'normal', True
        if 20 < odds <= 25:                           # ROI97%, 4/7年黒字
            return 'challenge', True
        return None, False

    if grade == 'G3':
        if 3 <= odds <= 16 and 3 <= gap <= 8 and heads >= 14:
            return 'normal', True
        return None, False

    if grade in ('G1', 'G2'):
        if 5 <= odds <= 20:                            # ROI167%(G2), 129%(G1)
            return 'challenge', True
        return None, False

    return None, False


# 後方互換: 旧コードがbool返却を期待する場合用
def _is_buy_v6_compat(grade, heads, gap, odds, ev7, good_train=True, sire=''):
    """旧互換: True/Falseのみ返す（通常+チャレンジ両方Trueにする）"""
    zone, buy = is_buy_v6(grade, heads, gap, odds, ev7, good_train, sire)
    return buy


KINGKAME_SIRES = frozenset({
    'キングカメハメハ','ロードカナロア','ルーラーシップ','ドレフォン','レイデオロ',
    'ホッコータルマエ','ロゴタイプ',
})


def is_special_buy(grade, odds, popularity, heads, accel, good_train, sire):
    """別枠買いルール: 調教×血統の特殊掛け合わせパターン

    スコアリング不要、全出走馬が対象。
    C2: 新馬×非主流血統×odds10-20×加速ラップ×15頭以上
        → 単勝1,100円 + ワイド(×人気1-3)各300円 = 2,000円/R (5/7年黒字)
    F1: 未勝利×主流血統(SS+KK)×odds20-30×好調教+加速ラップ×1-8番人気
        → 単勝1,000円のみ = 1,000円/R (4/6年黒字, ROI161%)

    Returns: (buy, rule_name) or (False, '')
    """
    if not accel:
        return False, ''
    sire_s = (sire or '').strip()
    is_mainstream = sire_s in SUNDAY_SIRES or sire_s in KINGKAME_SIRES

    # C2: 新馬×非主流血統×中穴×加速ラップ×多頭数
    if not is_mainstream:
        if grade == '新馬' and 10 <= odds < 20 and heads >= 15:
            return True, 'C2_新馬accel'

    # F1: 未勝利×主流血統(SS+KK)×穴馬×好調教+加速ラップ
    if is_mainstream and good_train:
        pop = popularity or 99
        if grade == '未勝利' and 20 <= odds < 30 and 1 <= pop <= 8:
            return True, 'F1_未勝利主流accel'

    return False, ''


def run_month_v6(conn, sc_conn, year, month):
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
        honmei_good = honmei.get('has_good_train', False)
        honmei_sire = honmei.get('_sire', '')
        buy_zone, ev_ok = is_buy_v6(gr, len(result), gap, honmei_odds or 0, ev7,
                                    good_train=honmei_good, sire=honmei_sire)

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
            'honmei_accel': honmei.get('accel_lap', False),
            'honmei_good_train': honmei.get('has_good_train', False),
            'ni_accel': ni.get('accel_lap', False),
        }
        # 全レースの払戻を計算
        d = race['date']; v = race['venue']; rn = race['race_num']
        pay_t, pay_u, pay_w, hits = get_payout_v3(
            conn, d, v, rn, honmei['horse_name'], ni['horse_name'])

        if buy_zone == 'challenge':
            # チャレンジ枠: 単勝1,000円のみ
            div_row_v6 = _get_div_cached(conn, d, v, rn)
            ret = 0
            if honmei['finish'] == 1 and div_row_v6:
                ret = div_row_v6['tansho_payout'] * 10
            rec['cost'] = 1000
            rec['ret'] = ret
            rec['profit'] = ret - 1000
            rec['buy_zone'] = 'challenge'
            rec['hits'] = '単勝的中' if honmei['finish'] == 1 else '不的中'
        else:
            # 通常: 単勝400+馬連300+ワイド300=1000円（v3互換）
            ret = pay_t + pay_u + pay_w
            rec['cost'] = 1000
            rec['ret'] = ret
            rec['profit'] = ret - 1000
            rec['buy_zone'] = 'normal' if ev_ok else None
            rec['hits'] = ' + '.join(hits) if hits else '全外れ'

        rec['ev7'] = ev7

        all_races.append(rec)
        if ev_ok:
            bets.append(rec)

        # ── 別枠ルール: 全出走馬を対象に調教×血統パターン判定 ──
        d = race['date']; v = race['venue']; rn = race['race_num']
        div_row = _get_div_cached(conn, d, v, rn)
        for h in result:
            h_odds = h.get('odds') or 0
            h_pop  = h.get('_popularity', 0)
            # popularityがresultに入っていない場合、rowsから取得
            if not h_pop:
                for rw in rows:
                    if str(rw.get('horse_name', '')).strip() == h['horse_name']:
                        h_pop = rw.get('popularity') or 0
                        break
            sp_buy, sp_rule = is_special_buy(
                gr, h_odds, h_pop, len(rows),
                h.get('accel_lap', False), h.get('has_good_train', False),
                h.get('_sire', ''))
            if not sp_buy:
                continue

            # ルール別の払戻計算
            sp_ret = 0
            if sp_rule == 'C2_新馬accel':
                # C2: 単勝1,000円のみ（ワイドはROI低下要因のため廃止）
                sp_cost = 1000
                if h['finish'] == 1 and div_row:
                    sp_ret = div_row['tansho_payout'] * 10
            elif sp_rule == 'F1_未勝利主流accel':
                # F1: 単勝1,000円のみ = 1,000円/R
                sp_cost = 1000
                if h['finish'] == 1 and div_row:
                    sp_ret = div_row['tansho_payout'] * 10
            else:
                sp_cost = 1000
                if h['finish'] == 1 and div_row:
                    sp_ret = div_row['tansho_payout'] * 10

            sp_rec = {
                'date': d, 'venue': v, 'race_num': rn, 'grade': gr,
                'heads': len(rows),
                'honmei_name': h['horse_name'],
                'honmei_odds': h_odds,
                'honmei_finish': h['finish'],
                'rule': sp_rule,
                'cost': sp_cost,
                'ret': sp_ret,
                'profit': sp_ret - sp_cost,
                'special': True,
            }
            bets.append(sp_rec)

    return all_races, bets


def run_year_v6(year, db_path):
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

        ar, br = run_month_v6(conn, sc_conn, year, month)
        all_races += ar
        bet_records += br
        sys.stdout.write(f'\r  {year}/{month:02d} 全{len(ar)}R 買{len(br)}R 累{time.time()-t0:.0f}s  ')
        sys.stdout.flush()
    print()
    conn.close(); sc_conn.close()
    return all_races, bet_records


if __name__ == '__main__':
    if '--year' not in sys.argv:
        print("Usage: python backtest_v6.py --year YYYY")
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
    print(f'  backtest_v6 [ゼロベース再設計]  {year}年')
    print(f'{"="*55}')

    t_start = time.time()
    all_races, bet_records = run_year_v6(year, tmp_db)
    elapsed = time.time() - t_start

    # cost可変対応の集計（v6本体=1000円固定、別枠=cost可変）
    for b in bet_records:
        if 'cost' not in b:
            b['cost'] = 1000  # v6本体はsummarize_v3互換で1000円

    total_inv = sum(b['cost'] for b in bet_records)
    total_ret = sum(b['ret'] for b in bet_records)
    total_roi = total_ret / total_inv * 100 if total_inv else 0
    total_prof = int(total_ret - total_inv)

    s = summarize_v3(year, all_races, bet_records)
    # cost可変の正確な値で上書き
    s['investment'] = total_inv
    s['profit'] = total_prof
    s['roi'] = round(total_roi, 1)
    s['elapsed_sec'] = round(elapsed, 1)

    fname = f'btv6_{year}.json'
    with open(fname, 'w', encoding='utf-8') as f:
        json.dump({'summary': s, 'bet_records': bet_records, 'all_races': all_races}, f,
                  ensure_ascii=False, default=str)

    Path(tmp_db).unlink(missing_ok=True)

    # タイプ別集計
    v6_bets = [b for b in bet_records if not b.get('special')]
    c2_bets = [b for b in bet_records if b.get('rule') == 'C2_新馬accel']
    f1_bets = [b for b in bet_records if b.get('rule') == 'F1_未勝利主流accel']

    print(f'\n  -- {year}年 結果 (v6.2+C2+F1) --')
    print(f'  全{s["total_races"]}R → 買{s["n_bet"]}R  投資{total_inv:,}円  ({elapsed:.0f}s)')
    print(f'  損益: {total_prof:+,}円   ROI: {total_roi:.1f}%')
    for label, sub in [('v6本体', v6_bets), ('C2新馬', c2_bets), ('F1未勝利', f1_bets)]:
        if not sub: continue
        si = sum(b['cost'] for b in sub)
        sr = sum(b['ret'] for b in sub)
        print(f'    {label}: {len(sub)}R  投資{si:,}  ROI={sr/si*100:.1f}%  損益{sr-si:+,}')
    print(f'  -> {fname} 保存済み')
