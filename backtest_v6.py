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
sys.stdout.reconfigure(encoding='utf-8')
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

# CLI フラグ (main で設定)
TANSHO_ONLY = False  # --tansho-only: 通常ゾーンを単勝2000円のみに変更
SANGACHI_ODDS_LO = 8  # --sangachi-10: 3勝通常ゾーン下限を8→10に変更
DIST_CHANGE_FILTER = False  # --dist-filter: 差し追(avg_pos4≥6)×600m超距離変更を見送り
FAMILY_NICKS_BONUS = False  # --family-nicks: SS×KK/ND 系統レベルニックスボーナス(0.3-0.5pt)
RACE_LEVEL_BONUS   = False  # --race-level: 前走レースレベルボーナス (race_level_index テーブル必要)
G2_NORMAL          = False  # --g2-normal: G2を challenge→normal化 (単勝1000→単勝+馬連2000)

# 調教フィルタ用: サンデーサイレンス系種牡馬リスト
SUNDAY_SIRES = frozenset({
    'ディープインパクト','ハーツクライ','キズナ','ステイゴールド','オルフェーヴル',
    'ゴールドシップ','ドゥラメンテ','スクリーンヒーロー','モーリス','エピファネイア',
    'ジャスタウェイ','リアルスティール','サトノダイヤモンド','キタサンブラック',
    'ワールドプレミア','サトノクラウン','スワーヴリチャード','シルバーステート',
    'ブラックタイド','マカヒキ','ダイワメジャー','ネオユニヴァース',
})


def _is_dist_change_filtered(conn, horse_name, race_date, cur_dist):
    """距離変更補正フィルタ: 差し追×600m超距離変更をTrue（見送り）"""
    prev = conn.execute(
        'SELECT distance FROM results WHERE TRIM(horse_name)=TRIM(?)'
        ' AND date < ? AND distance IS NOT NULL ORDER BY date DESC LIMIT 1',
        (horse_name, race_date)
    ).fetchone()
    if not prev or prev[0] is None:
        return False
    if abs(cur_dist - prev[0]) < 600:
        return False
    pos_rows = conn.execute(
        'SELECT CAST(pos4 AS REAL) / CAST(num_horses AS REAL)'
        ' FROM results WHERE TRIM(horse_name)=TRIM(?)'
        ' AND date < ? AND pos4 IS NOT NULL AND num_horses > 0'
        ' ORDER BY date DESC LIMIT 5',
        (horse_name, race_date)
    ).fetchall()
    if not pos_rows:
        return False
    avg_norm = sum(r[0] for r in pos_rows) / len(pos_rows) * 10
    return avg_norm >= 6.0


def is_bias_disadvantaged(venue, surface, week_num, honmei_num, num_horses):
    """Top3強バイアス不利枠フィルタ（案Bフィルタ方式）
    スコアリングは変えず、買い判定後にhonmeiが不利枠なら見送り。
    対象: ROI差-50pt以上の3パターンのみ。
    """
    if num_horses < 8 or honmei_num <= 0:
        return False
    ratio = honmei_num / num_horses
    phase = '前半' if week_num <= 3 else ('中盤' if week_num <= 5 else '後半')
    # 内枠(ratio<=0.35)が不利な3パターン
    if ratio <= 0.35:
        if venue == '新潟' and surface == '芝' and phase == '後半':   return True  # ROI差-68.6pt
        if venue == '札幌' and surface == '芝' and phase == '後半':   return True  # ROI差-63.1pt
        if venue == '中山' and surface == 'ダ' and phase == '前半':   return True  # ROI差-53.1pt
    return False


def is_buy_v6(grade, heads, gap, odds, ev7, good_train=True, sire='', track_cond='', accel=False, train_count_7d=99):
    """v6.6 通常買い条件 + 調教フィルタ + 不良馬場フィルタ + 3勝accel必須

    Args:
        accel: 加速ラップフラグ (lap1 < lap2)。3勝クラスで必須。
        train_count_7d: 直近7日調教本数。2本未満は見送り (2026-04-23採用, +3.8pt Win)。
                        デフォルト99=未チェック(旧コード互換)。

    Returns: ('normal', True/False) for 通常ゾーン
             ('challenge', True/False) for チャレンジゾーン
             (None, False) for 見送り
    """
    if track_cond in ('不', '不良'): return None, False  # 不良馬場は買わない
    if train_count_7d < 2: return None, False           # 直近7日調教2本未満は見送り
    if odds is None or odds == 0: return None, False
    if not (2.0 <= ev7 <= 20.0): return None, False

    if grade == '新馬': return None, False
    if grade == '未勝利': return None, False

    if grade == '1勝':
        # 【v6.6】1勝クラス完全廃止（2026-04-13 実運用性レビューで決定）
        # 細帯分析の結果、4.0-4.5倍のみが黒字(+8,800)だったが、
        # この0.5倍幅はオッズ変動(平均±0.3-0.5)で3回に2回は圏外になる
        # 非現実的な狭帯。バックテスト性能は架空の「最終オッズが完璧に4.0-4.5
        # に入ったケース」を仮定しており実運用では再現不可能。
        # 廃止効果: 1256R/113.2%/+199,960 (vs B2 1291R/112.6%/+199,260)
        # 件数-35R, ROI +0.6pt, 実運用性100%確保。
        return None, False

    if grade == '2勝':
        # 【v6.6】2勝クラス廃止（2026-04-13検証で決定）
        # 7年バックテストで単独赤字、的中率0-6%の構造的不振クラス
        # 廃止により全体損益: -314,400 → -51,210 (+263,190改善)
        # ROI: 88.6% → 97.9% (+9.3pt)
        return None, False

    if grade == '3勝':
        # 【v6.6】3勝クラス
        # - normal帯: 8-11倍 (細帯分析で他帯は赤字)
        # - accel(加速ラップ)必須: accel=True 113R/+90,900 vs accel=False 80R/-80,300
        # - 効果: ROI 121.4→127.0% (+68,990円)
        if not accel:
            return None, False
        if sire in SUNDAY_SIRES and not good_train:
            return None, False
        cond_a = (heads >= 12)
        cond_b = (gap >= 8)
        if not (cond_a or cond_b): return None, False
        if SANGACHI_ODDS_LO <= odds <= 11 and (cond_a or cond_b):
            return 'normal', True
        if 20 < odds <= 25:                           # challenge ROI97%
            return 'challenge', True
        return None, False

    if grade == 'G3':
        # 【v6.6】G3完全廃止（2026-04-13検証で決定）
        # 58R/7年の小サンプルで勝率13.8% ROI 65.2% -42,590円
        # 7-9倍で15R中0勝、13-16倍で9R中0勝と構造的不振
        # 廃止効果: +42,590円, ROI 113.2→117.4%
        # CV: 5/6正解（2020の単発黒字年のみ揺れる）
        return None, False

    if grade in ('G1', 'G2'):
        # 【v6.6】内部赤字帯 5-7 と 10-13 を除外
        # 細帯分析: 5-7倍 -3,600🔴 / 7-10倍 +7,700🟢 / 10-13倍 -9,500🔴
        #          13-16倍 +6,300🟢 / 16-20倍 +17,800🟢
        # 絞込効果: +14,500円 (G2 79R→48R)
        if 7 <= odds < 10 or 13 <= odds <= 20:
            # --g2-normal: G2のみ normalゾーン扱い(単勝+馬連2000円)に変更
            if G2_NORMAL and grade == 'G2':
                return 'normal', True
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


def is_special_buy(grade, odds, popularity, heads, accel, good_train, sire, surface=''):
    """別枠買いルール: 調教×血統の特殊掛け合わせパターン

    スコアリング不要、全出走馬が対象。
    C2: 新馬×非主流血統×odds10-20×加速ラップ×15頭以上×ダート限定
        → 単勝1,000円のみ (6/7年黒字, ROI135%)
    F1: 未勝利×主流血統(SS+KK)×odds20-30×好調教+加速ラップ×1-8番人気
        → 単勝1,000円のみ (4/6年黒字, ROI175%)

    Returns: (buy, rule_name) or (False, '')
    """
    if not accel:
        return False, ''
    sire_s = (sire or '').strip()
    is_mainstream = sire_s in SUNDAY_SIRES or sire_s in KINGKAME_SIRES

    # C2: 新馬×非主流血統×中穴×加速ラップ×多頭数×ダート限定
    if not is_mainstream:
        surf = str(surface or '')
        is_dirt = 'ダ' in surf
        if grade == '新馬' and 10 <= odds < 20 and heads >= 15 and is_dirt:
            return True, 'C2_新馬accel'

    # F1: 未勝利×主流血統(SS+KK)×穴馬×好調教+加速ラップ
    if is_mainstream and good_train:
        pop = popularity or 99
        # 【v6.6】F1未勝利 odds 15-33
        # Phase 2b: 20-30 → 15-35 に拡張 (+52,300)
        # B3: 33-35帯は24R中0勝で除外、15-33に絞り (+45,610)
        if grade == '未勝利' and 15 <= odds < 33 and 1 <= pop <= 8:
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
        honmei_accel = honmei.get('accel_lap', False)
        tc_row = conn.execute(
            "SELECT COUNT(*) FROM training WHERE TRIM(horse_name)=TRIM(?)"
            " AND date>=DATE(?,'-7 days') AND date<?",
            (honmei['horse_name'], race['date'], race['date'])
        ).fetchone()
        honmei_train_count = tc_row[0] if tc_row else 0
        buy_zone, ev_ok = is_buy_v6(gr, len(result), gap, honmei_odds or 0, ev7,
                                    good_train=honmei_good, sire=honmei_sire,
                                    accel=honmei_accel, train_count_7d=honmei_train_count)

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
            div_row_v6 = _get_div_cached(conn, d, v, rn)
            if TANSHO_ONLY:
                # 単勝のみモード: 2000円全額単勝
                t_bet, u_bet = 2000, 0
                ret = 0
                hit_parts = []
                if div_row_v6 and honmei['finish'] == 1:
                    ret += div_row_v6['tansho_payout'] * (t_bet / 100)
                    hit_parts.append('単勝')
                ret = int(ret)
                rec['cost'] = t_bet
            else:
                # 通常ゾーン: オッズ帯別配分（あかり案A）
                # ◎低オッズ(< 8倍): 単勝1000 + 馬連1000 = 2000円
                # ◎高オッズ(>= 8倍): 単勝500 + 馬連1500 = 2000円
                ho = honmei_odds or 0
                if ho >= 8:
                    t_bet, u_bet = 500, 1500  # 高オッズ: 馬連寄せ
                else:
                    t_bet, u_bet = 1000, 1000  # 低オッズ: 現行
                ret = 0
                hit_parts = []
                if div_row_v6:
                    if honmei['finish'] == 1:
                        ret += div_row_v6['tansho_payout'] * (t_bet / 100)
                        hit_parts.append('単勝')
                    if div_row_v6['umaren_payout'] and (
                        (honmei['finish'] == 1 and ni['finish'] == 2) or
                        (honmei['finish'] == 2 and ni['finish'] == 1)):
                        ret += div_row_v6['umaren_payout'] * (u_bet / 100)
                        hit_parts.append('馬連')
                ret = int(ret)
                rec['cost'] = t_bet + u_bet
            rec['ret'] = ret
            rec['profit'] = ret - rec['cost']
            rec['buy_zone'] = 'normal' if ev_ok else None
            rec['hits'] = '+'.join(hit_parts) if hit_parts else '不的中'

        rec['ev7'] = ev7

        all_races.append(rec)
        if ev_ok:
            # フィルタ方式: 強バイアス不利枠なら見送り（スコアリングは変えない）
            surface = rows[0].get('surface', '芝')
            if '芝' in str(surface): surface = '芝'
            else: surface = 'ダ'
            wn = rows[0].get('week_num', 0) or 0
            bias_skip = is_bias_disadvantaged(race['venue'], surface, wn,
                                              honmei['horse_num'], len(rows))
            dist_skip = (DIST_CHANGE_FILTER and not bias_skip and
                         bool((rows[0].get('distance') or 0) and
                              _is_dist_change_filtered(conn, honmei['horse_name'],
                                                       race['date'], rows[0]['distance'])))
            if bias_skip:
                rec['bias_filtered'] = True  # 記録は残す（分析用）
            elif dist_skip:
                rec['dist_filtered'] = True  # 距離変更フィルタで除外
            else:
                bets.append(rec)

                # ── 3連単 ◎○BOX→人気1-5 (gap5+ & odds8+のみ) ──
                ho = honmei_odds or 0
                if gap >= 5 and ho >= 8:
                    div3 = _get_div_cached(conn, race['date'], race['venue'], race['race_num'])
                    if div3 and div3.get('sanrentan_payout'):
                        h_uma = honmei.get('horse_num', 0)
                        n_uma = ni.get('horse_num', 0)
                        # 人気上位3頭(◎○除く)の馬番を取得
                        pop_rows = conn.execute(
                            'SELECT horse_num FROM results WHERE date=? AND venue=? AND race_num=? AND finish > 0 AND finish < 90 ORDER BY popularity',
                            (race['date'], race['venue'], race['race_num'])).fetchall()
                        top3_uma = [r2['horse_num'] for r2 in pop_rows if r2['horse_num'] not in (h_uma, n_uma)][:3]
                        axis = {h_uma, n_uma}
                        all_set = axis | set(top3_uma)
                        san = [div3.get('sanrentan_uma1',0), div3.get('sanrentan_uma2',0), div3.get('sanrentan_uma3',0)]
                        # 1着が◎○、2-3着がall_setの中
                        if san[0] in axis and san[1] in all_set and san[2] in all_set:
                            san_ret = div3['sanrentan_payout'] * 1  # 100円×1
                        else:
                            san_ret = 0
                        pts = 24  # 1着◎○(2) × 2着5頭 × 3着4頭 = 24点
                        san_cost = pts * 100
                        san_rec = {
                            'date': race['date'], 'venue': race['venue'],
                            'race_num': race['race_num'], 'grade': gr,
                            'heads': len(rows),
                            'honmei_name': honmei['horse_name'],
                            'honmei_odds': ho,
                            'honmei_finish': honmei['finish'],
                            'rule': '3rentan_box',
                            'cost': san_cost,
                            'ret': san_ret,
                            'profit': san_ret - san_cost,
                            'special': True,
                        }
                        bets.append(san_rec)

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
                h.get('_sire', ''), surface=rows[0].get('surface', ''))
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
    # blood_start: 全期間データを使用（直近3年は悪化したため不採用）
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
        print("Usage: python backtest_v6.py --year YYYY [--tansho-only]")
        sys.exit(1)

    if '--tansho-only' in sys.argv:
        TANSHO_ONLY = True  # noqa: F841  (module-level global)
    if '--sangachi-10' in sys.argv:
        SANGACHI_ODDS_LO = 10  # noqa: F841  (module-level global)
    if '--dist-filter' in sys.argv:
        DIST_CHANGE_FILTER = True  # noqa: F841  (module-level global)
    if '--g2-normal' in sys.argv:
        G2_NORMAL = True  # noqa: F841  (module-level global)
    if '--family-nicks' in sys.argv:
        FAMILY_NICKS_BONUS = True  # noqa: F841  (module-level global)
        os.environ['NORISHIKO_FAMILY_NICKS'] = '1'
    if '--race-level' in sys.argv:
        RACE_LEVEL_BONUS = True  # noqa: F841  (module-level global)
        import scoring as _sc
        _sc.RACE_LEVEL_ENABLED = True
        variant_flags = [a for a in sys.argv if a.startswith('--rl-variant=')]
        if variant_flags:
            _sc.RACE_LEVEL_VARIANT = variant_flags[-1].split('=')[1]
        print(f"[race-level] variant={_sc.RACE_LEVEL_VARIANT}")

    idx = sys.argv.index('--year')
    year = int(sys.argv[idx + 1])

    src_db = 'keiba.db'
    tmp_db = f'keiba_tmp_{year}.db'
    # NEW2: 常にfreshコピー (古いtmp_dbの再利用による汚染を防止)
    if Path(tmp_db).exists():
        Path(tmp_db).unlink()
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

    # NEW1: Outlier耐性メトリクス (Winsorized ROI)
    # 1レースあたりの return を 50,000円 でキャップ → メガヒット1件に依存しない
    WINSORIZE_CAP = 50000
    win_ret = sum(min(b['ret'], WINSORIZE_CAP) for b in bet_records)
    win_roi = win_ret / total_inv * 100 if total_inv else 0
    win_prof = int(win_ret - total_inv)
    outlier_count = sum(1 for b in bet_records if b['ret'] > WINSORIZE_CAP)

    s = summarize_v3(year, all_races, bet_records)
    # cost可変の正確な値で上書き
    s['investment'] = total_inv
    s['profit'] = total_prof
    s['roi'] = round(total_roi, 1)
    s['winsorized_roi'] = round(win_roi, 1)
    s['winsorized_profit'] = win_prof
    s['outlier_count'] = outlier_count
    s['elapsed_sec'] = round(elapsed, 1)

    suffix = '_tansho' if TANSHO_ONLY else ('_s10' if SANGACHI_ODDS_LO == 10 else ('_distf' if DIST_CHANGE_FILTER else ('_fnicks' if FAMILY_NICKS_BONUS else ('_g2n' if G2_NORMAL else ''))))
    fname = f'btv6_{year}{suffix}.json'
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
    if outlier_count:
        print(f'  [Winsorized] 損益: {win_prof:+,}円  ROI: {win_roi:.1f}%  (>{WINSORIZE_CAP:,}円キャップ {outlier_count}件)')
    for label, sub in [('v6本体', v6_bets), ('C2新馬', c2_bets), ('F1未勝利', f1_bets)]:
        if not sub: continue
        si = sum(b['cost'] for b in sub)
        sr = sum(b['ret'] for b in sub)
        print(f'    {label}: {len(sub)}R  投資{si:,}  ROI={sr/si*100:.1f}%  損益{sr-si:+,}')
    print(f'  -> {fname} 保存済み')
