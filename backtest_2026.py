"""
ノリシコ競馬AI / backtest_2026.py
2026年データでのバックテスト

賭け戦略:
  ◎ 単勝: EV判定OK（standout_gap≥8 かつ odds 3-30倍 かつ EV+5%以上）
  全レース結果も集計してスコアリング精度を検証

使い方:
    python backtest_2026.py
    python backtest_2026.py --year 2025   # 過去年でも可
"""

import sqlite3
import numpy as np
import pandas as pd
import sys
import os
import json
import shutil
from pathlib import Path
from collections import defaultdict

DB_PATH  = "keiba.db"
YEAR     = 2026

# ══════════════════════════════════════════════════════════════
# 依存ファイルのセットアップ
# ══════════════════════════════════════════════════════════════
def setup_files():
    """scoring.py が必要とするJSONファイルを作業ディレクトリに配置"""
    for fname in ['gate_style_bias.json', 'gate_cond_blood_stats.json']:
        if not Path(fname).exists():
            # 同ディレクトリか uploads を探す
            for src in [f'./{fname}', f'/mnt/user-data/uploads/{fname}']:
                if Path(src).exists():
                    shutil.copy(src, fname)
                    break

setup_files()

import scoring as sc
from scoring import (
    get_conn, score_past_performance, score_course_fitness,
    score_jockey_trainer, score_rotation, score_training_actual,
    score_bloodline, score_gate_style, get_weights,
    score_surface_switch, _surface_switch_cache,
    calc_pace_context, _infer_running_style,
    calc_course_blood_bonus, calc_gate_cond_blood_bonus,
    calc_track_bias_bonus, calc_venue_sire_bonus,
    calc_venue_damsire_bonus, calc_cushion_sire_bonus,
    calc_nicks_bonus, calc_family_nicks_bonus,
    calc_daily_bias_bonus, calc_condition_penalty, EV_CONDITIONS,
    calc_race_level_bonus, calc_lap_pace_bonus,
    _past_runs_cache, _course_runs_cache, _running_style_cache,
    _jockey_cache, _trainer_cache, _combo_cache, _ace_cache,
    _avg_time_cache, _last3f_cache, _training_actual_cache,
    _bloodline_score_cache, _week_cache, _wet_perf_cache,
)


# ══════════════════════════════════════════════════════════════
# 配当取得
# ══════════════════════════════════════════════════════════════
def get_dividends(conn, date, venue, race_num):
    row = conn.execute(
        "SELECT * FROM dividends WHERE date=? AND venue=? AND race_num=?",
        (date, venue, race_num)
    ).fetchone()
    if not row:
        return {}
    return dict(row)


def get_tansho_payout(conn, date, venue, race_num, horse_num):
    """単勝払戻を取得（円単位）"""
    div = get_dividends(conn, date, venue, race_num)
    if not div:
        # dividends がなければ results から推定（odds × 100）
        row = conn.execute(
            "SELECT odds FROM results WHERE date=? AND venue=? AND race_num=? AND finish=1",
            (date, venue, race_num)
        ).fetchone()
        if row and row['odds']:
            return int(row['odds'] * 100)
        return None

    # tansho_payout は 10円単位で格納 → 100円換算
    pay = div.get('tansho_payout')
    if pay and int(pay) > 0:
        return int(pay) * 10  # 10円単位→円
    return None


# ══════════════════════════════════════════════════════════════
# プリフェッチ（高速化）
# ══════════════════════════════════════════════════════════════
def clear_caches(full=False):
    # avg_time_cache / last3f_cache / _week_cache は prefetch_score_caches で一括ロード済みなのでクリアしない
    for c in [_training_actual_cache, _running_style_cache,
              _past_runs_cache, _course_runs_cache,
              _bloodline_score_cache, _wet_perf_cache,
              _surface_switch_cache]:
        c.clear()
    if full:
        for c in [_jockey_cache, _trainer_cache, _combo_cache, _ace_cache]:
            c.clear()


def prefetch_month(conn, year, month):
    """月内データの一括プリフェッチ"""
    d_from = f'{year}-{month:02d}-01'
    d_to   = f'{year}-{month:02d}-31'
    ym     = f'{year}-{month:02d}'

    # 過去走
    horse_list = [r['horse_name'] for r in conn.execute(
        "SELECT DISTINCT horse_name FROM results WHERE date BETWEEN ? AND ? AND finish<90",
        (d_from, d_to)
    ).fetchall()]
    if not horse_list:
        return

    ph = ','.join(['?'] * len(horse_list))

    # past_runs_cache
    # バグ修正(2026-04-13): race_name がSELECTに無く、scoring.py L517の
    # _grade_from_race_name(row.get('race_name','')) が常に空文字を返していた。
    # → 過去走スコアの上がり3F成分が全走で同じクラス扱いになり歪んでいた。
    rows_pp = conn.execute(f"""
        SELECT horse_name, finish, time_sec, last3f, surface, distance,
               track_cond, venue, num_horses, date, horse_weight, margin, race_num, pos4,
               race_name
        FROM results
        WHERE horse_name IN ({ph}) AND date < ? AND finish < 90
        ORDER BY horse_name, date DESC
    """, horse_list + [d_from]).fetchall()

    from collections import defaultdict
    pp_map = defaultdict(list)
    for r in rows_pp:
        pp_map[r['horse_name']].append(dict(r))

    for horse in horse_list:
        key = (horse, ym)
        if key not in _past_runs_cache:
            _past_runs_cache[key] = pp_map[horse][:5]

    # course_runs_cache
    cf_rows = conn.execute(f"""
        SELECT horse_name, finish, surface, distance, venue
        FROM results
        WHERE horse_name IN ({ph}) AND date < ? AND finish < 90
    """, horse_list + [d_from]).fetchall()

    cf_map = defaultdict(list)
    for r in cf_rows:
        db = r['distance'] // 400
        cf_map[(r['horse_name'], r['surface'], db)].append({
            'finish': r['finish'], 'distance': r['distance'],
            'venue': r['venue'], 'v': r['venue'], 'surface': r['surface'],
        })

    combos = conn.execute(
        "SELECT DISTINCT horse_name, surface, distance FROM results WHERE date BETWEEN ? AND ? AND finish<90",
        (d_from, d_to)
    ).fetchall()
    for c in combos:
        key = (c['horse_name'], ym, c['surface'], c['distance'] // 400)
        if key not in _course_runs_cache:
            matches = [r for r in cf_map.get((c['horse_name'], c['surface'], c['distance']//400), [])
                       if abs(r['distance'] - c['distance']) <= 800]
            _course_runs_cache[key] = matches[:10]

    # running_style_cache
    style_rows = conn.execute(f"""
        SELECT horse_name, date, pos4, num_horses
        FROM results WHERE horse_name IN ({ph}) AND date < ?
          AND pos4 > 0 AND num_horses > 0 AND finish < 90
        ORDER BY horse_name, date DESC
    """, horse_list + [d_from]).fetchall()

    style_map = defaultdict(list)
    for r in style_rows:
        style_map[r['horse_name']].append(r)

    for horse in horse_list:
        cache_key = (horse, ym)
        if cache_key not in _running_style_cache:
            runs = style_map[horse][:3]
            if not runs:
                _running_style_cache[cache_key] = None
            else:
                ratios = [r['pos4'] / r['num_horses'] for r in runs]
                avg = sum(ratios) / len(ratios)
                if   avg <= 0.20: style = '逃げ'
                elif avg <= 0.45: style = '先行'
                elif avg <= 0.70: style = '中団'
                else:             style = '差追'
                _running_style_cache[cache_key] = style

    # training_actual_cache
    # lap2 もSELECTして accel_lap 判定に使う（加速ラップ: lap1 < lap2）
    # training.horse_name は末尾スペース混入歴があるためTRIM照合（DB罠ルール）
    t_rows = conn.execute(f"""
        SELECT horse_name, date, lap1, lap2, source
        FROM training
        WHERE TRIM(horse_name) IN ({ph})
          AND date BETWEEN date(?, '-14 days') AND ?
          AND lap1 IS NOT NULL AND lap1 > 0
        ORDER BY horse_name, lap1 ASC
    """, [(h or '').strip() for h in horse_list] + [d_from, d_to]).fetchall()

    t_map = defaultdict(list)
    for r in t_rows:
        t_map[(r['horse_name'] or '').strip()].append(r)

    # レース日マップを一括取得（馬ごとの個別SQLを排除）
    rd_rows = conn.execute(f"""
        SELECT DISTINCT horse_name, date FROM results
        WHERE horse_name IN ({ph}) AND date BETWEEN ? AND ? AND finish<90
    """, horse_list + [d_from, d_to]).fetchall()
    horse_race_dates = defaultdict(list)
    for r in rd_rows:
        horse_race_dates[r['horse_name']].append(r['date'])

    for horse in horse_list:
        for race_date in horse_race_dates.get(horse, []):
            key = (horse, race_date)
            if key not in _training_actual_cache:
                candidates = [r for r in t_map[(horse or '').strip()] if r['date'] < race_date]
                if candidates:
                    best = min(candidates, key=lambda x: x['lap1'])
                    lap1 = best['lap1']
                    lap2 = best['lap2'] if 'lap2' in best.keys() else None
                    src  = best['source'] or 'woodc'
                    if src == 'woodc':
                        if   lap1 < 11.0: score = 95.0
                        elif lap1 < 11.3: score = 90.0
                        elif lap1 < 11.5: score = 75.0
                        elif lap1 < 11.7: score = 60.0
                        else:             score = 50.0
                        good = lap1 < 11.5
                    else:
                        if   lap1 < 11.3: score = 95.0
                        elif lap1 < 11.6: score = 90.0
                        elif lap1 < 12.0: score = 75.0
                        elif lap1 < 12.5: score = 60.0
                        else:             score = 50.0
                        good = lap1 < 12.0
                    # 加速ラップ判定（lap1 < lap2 = 末脚が速い = 仕上がり良好）
                    # バグ修正: 以前はaccel_lapがキャッシュに入っておらず、
                    # score_training_actualのキャッシュヒット時にaccel_lap=Falseになり
                    # C2新馬・F1未勝利ルールが完全に発動しない重大な欠陥があった
                    accel = bool(lap2 is not None and lap2 > 0 and lap1 < lap2)
                    _training_actual_cache[key] = {
                        'score': score,
                        'has_good_train': good,
                        'accel_lap': accel,
                    }
                else:
                    _training_actual_cache[key] = {
                        'score': 48.0,
                        'has_good_train': False,
                        'accel_lap': False,
                    }


def grade_from_name(name):
    import re
    s = str(name)
    for g in ['G1', 'G2', 'G3']:
        if g in s: return g
    if '3勝' in s or '３勝' in s or '1600万' in s: return '3勝'
    if '2勝' in s or '２勝' in s or '1000万' in s: return '2勝'
    if '1勝' in s or '１勝' in s or '500万' in s:  return '1勝'
    if '新馬' in s: return '新馬'
    if '未勝利' in s: return '未勝利'
    return ''


# ══════════════════════════════════════════════════════════════
# 1レーススコアリング
# ══════════════════════════════════════════════════════════════
def score_one_race(race_rows, sc_conn):
    """1レースのスコアリングを実行してリストを返す"""
    if not race_rows or len(race_rows) < 3:
        return []

    date    = race_rows[0]['date']
    venue   = race_rows[0]['venue']
    race_num= race_rows[0]['race_num']
    surf    = race_rows[0]['surface']
    dist    = int(race_rows[0]['distance'])
    cond    = race_rows[0]['track_cond']
    rname   = race_rows[0]['race_name']
    heads   = len(race_rows)

    # クッション値 (芝レースのみ有効、父血統ボーナスに使用)
    cushion_row = sc_conn.execute(
        "SELECT cushion FROM cushion_value WHERE date=? AND venue=?", (date, venue)
    ).fetchone()
    cushion = cushion_row['cushion'] if cushion_row else None

    gr = grade_from_name(rname)
    W  = get_weights(gr)

    # 展開コンテキスト
    race_styles = [
        _infer_running_style(str(r['horse_name']).strip(), date, surf, dist, sc_conn)
        for r in race_rows
    ]
    pci_row = sc_conn.execute("""
        SELECT AVG(pci) as avg_pci FROM (
            SELECT pci FROM race_pace
            WHERE venue=? AND surface=? AND distance=?
              AND date < ? AND pci IS NOT NULL
            ORDER BY date DESC LIMIT 5
        )
    """, (venue, surf, dist, date)).fetchone()
    recent_pci = float(pci_row['avg_pci']) if pci_row and pci_row['avg_pci'] else None
    pace_ctx   = calc_pace_context(race_styles, recent_pci=recent_pci)
    pace_mult  = pace_ctx['mult']

    res = []
    for _ri, row in enumerate(race_rows):
        h  = str(row['horse_name']).strip()
        hn = int(row['horse_num']) if row['horse_num'] else 0
        umaban_real = int(row['umaban']) if row.get('umaban') else 0
        j  = str(row['jockey']).strip()
        tr = str(row['trainer']).strip()
        si = str(row['sire'] or '').strip()
        ds = str(row['dam_sire'] or '').strip()

        hw = int(row['horse_weight']) if row.get('horse_weight') and row['horse_weight'] > 0 else 0
        ym = date[:7]
        prev_runs = _past_runs_cache.get((h, ym), [])
        wc = None
        if hw > 0 and prev_runs and prev_runs[0].get('horse_weight', 0) > 0:
            wc = hw - prev_runs[0]['horse_weight']

        interval_weeks = None
        if prev_runs:
            last_d = prev_runs[0].get('date')
            if last_d:
                try:
                    from datetime import datetime
                    interval_weeks = (datetime.strptime(date, '%Y-%m-%d') -
                                      datetime.strptime(last_d, '%Y-%m-%d')).days / 7.0
                except:
                    pass

        sp  = score_past_performance(h, date, surf, dist, sc_conn)
        sc2 = score_course_fitness(h, date, surf, dist, cond, sc_conn, venue)
        sj  = score_jockey_trainer(j, tr, date, surf, dist, sc_conn, h, grade=gr)
        sr  = score_rotation(interval_weeks, row.get('prev_finish'), row.get('prev_distance'),
                             dist, gr, horse_weight=hw, surface=surf, weight_change=wc)
        sb  = score_bloodline(si, ds, date, surf, dist, sc_conn)
        st  = score_training_actual(h, date, sc_conn)
        sg  = score_gate_style(h, hn, date, venue, surf, dist, sc_conn, pace_mult)

        # 初ダート/初芝 転向補正
        s_switch = score_surface_switch(h, date, surf, dist, sc_conn, sire=si, dam_sire=ds)
        if s_switch:
            sp['score']  = max(0, min(100, sp['score']  + s_switch['past_adj']))
            sc2['score'] = max(0, min(100, sc2['score'] + s_switch['course_adj']))

        total = (
            sp['score']    * W['past_performance'] +
            sc2['score']   * W['course_fitness'] +
            sj['score']    * W['jockey_trainer'] +
            sr['score']    * W['rotation'] +
            st['score']    * W['training'] +
            sb['score']    * W['sire'] +
            sb['dam_sire'] * W['dam_sire'] +
            sg['score']    * W['gate_style']
        )

        res.append({
            'horse_name':   h,
            'horse_num':    hn,
            'umaban':       umaban_real,
            'jockey':       j,
            'finish':       int(row['finish']) if row['finish'] and row['finish'] < 90 else 99,
            'odds':         float(row['odds']) if row.get('odds') else None,
            'total_score':  round(total, 1),
            '_blood_score': sb['score'],
            '_sire':        si,
            '_dam_sire':    ds,
            '_prev_pos4':   int(prev_runs[0]['pos4']) if prev_runs and prev_runs[0].get('pos4') else 0,
            '_running_style': race_styles[_ri] if _ri < len(race_styles) else '',
            'accel_lap':    st.get('accel_lap', False),
            'has_good_train': st.get('has_good_train', False),
        })

    # 決定論性のため、同点ブレイクで馬名を安定キーに使用
    # (Pythonの安定ソートでも入力順の状態次第で結果が揺れる問題対策)
    res.sort(key=lambda x: (-x['total_score'], x['horse_name']))

    # 血統ランク → 相乗ボーナス
    blood_sorted = sorted(res, key=lambda x: (-x['_blood_score'], x['horse_name']))
    for rank, h2 in enumerate(blood_sorted, 1):
        h2['_blood_rank'] = rank

    for h2 in res:
        bonus = calc_course_blood_bonus(h2['horse_name'], date, venue, surf, dist,
                                        h2['_blood_rank'], sc_conn)
        gcbb  = calc_gate_cond_blood_bonus(h2['horse_name'], date, venue, surf, dist,
                                           h2['horse_num'], heads, cond, h2['_sire'], sc_conn)
        tbb   = calc_track_bias_bonus(venue, surf, date, h2['horse_num'], heads,
                                       h2['_prev_pos4'], sc_conn)
        vsb   = calc_venue_sire_bonus(venue, dist, h2['_sire'], sc_conn)
        vdsb  = calc_venue_damsire_bonus(venue, dist, h2['_dam_sire'], sc_conn)
        csb   = calc_cushion_sire_bonus(cushion, h2['_sire'], surf, sc_conn)
        nkb   = calc_nicks_bonus(h2['_sire'], h2['_dam_sire'], surf, sc_conn)
        fnkb  = calc_family_nicks_bonus(h2['_sire'], h2['_dam_sire'], surf, sc_conn)
        rlb   = calc_race_level_bonus(h2['horse_name'], date, sc_conn)
        h2['total_score'] = round(h2['total_score'] + bonus + gcbb + tbb + vsb + vdsb + csb + nkb + fnkb + rlb, 1)
        # りさ戦略用: 血統・調教・コースボーナスの非ゼロ数（データの充実度）
        h2['_bd_nonzero'] = sum(1 for v in [bonus, gcbb, tbb, vsb, vdsb, csb, nkb, fnkb, rlb,
            h2.get('_blood_score', 0), h2.get('accel_lap') or 0, h2.get('has_good_train') or 0]
            if v and v > 0)

    res.sort(key=lambda x: (-x['total_score'], x['horse_name']))
    for rank, h2 in enumerate(res, 1):
        h2['rank'] = rank

    # EV計算 (M1: softmax温度パラメータ)
    # M1: softmax温度 T=11 (sweep T=6-15で4年Win最適化、+24,750改善)
    SOFTMAX_TEMP = float(os.environ.get('NORISHIKO_SOFTMAX_TEMP', '11'))
    scores = np.array([h2['total_score'] for h2 in res])
    exp_s  = np.exp((scores - scores.mean()) / SOFTMAX_TEMP)
    probs  = exp_s / exp_s.sum()
    gap    = round(res[0]['total_score'] - res[1]['total_score'], 1) if len(res) > 1 else 0

    MAX_ODDS = EV_CONDITIONS['max_odds']
    for h2, p in zip(res, probs):
        h2['win_prob'] = round(float(p), 4)
        o = h2.get('odds') or 0
        h2['ev'] = round((p * o - 1) * 100, 1) if 0 < o <= MAX_ODDS else None
        h2['standout_gap'] = gap

    # EV判定（本命馬）
    honmei = res[0]
    h_odds = honmei.get('odds')
    ok = (
        gap >= EV_CONDITIONS['standout_gap'] and
        h_odds is not None and
        EV_CONDITIONS['min_odds'] <= h_odds <= MAX_ODDS and
        honmei['ev'] is not None and
        honmei['ev'] >= EV_CONDITIONS['min_ev_pct']
    )
    for h2 in res:
        h2['ev_ok'] = ok

    return res, {
        'date': date, 'venue': venue, 'race_num': race_num,
        'race_name': rname, 'surface': surf, 'distance': dist,
        'grade': gr, 'heads': heads, 'standout_gap': gap,
        'nige_count': pace_ctx['nige_count'],
        'recent_pci': recent_pci,
    }


# ══════════════════════════════════════════════════════════════
# バックテストメイン
# ══════════════════════════════════════════════════════════════
def run_backtest(year=YEAR, db_path=DB_PATH, yukiko_gap=None, risa_nonzero=None):
    print(f"\n{'═'*65}")
    print(f"  ノリシコ競馬AI — バックテスト {year}年")
    print(f"{'═'*65}\n")
    # ⚠️ リーク警告: このrun_backtest単体では血統/ニックス/クッション等のボーナステーブルを
    # cutoff付きで再構築しない。DBに全期間ビルド(cutoff=2099)のテーブルが残っていると
    # BT期間より未来の集計値でスコアリングされる（look-ahead）。
    # 正式なBTは backtest_v6.py 経由（年次cutoffで再構築される）を使うこと。
    print("⚠️ 警告: backtest_2026 単体実行はボーナステーブルのcutoff再構築を行いません。")
    print("   全期間ビルドのテーブルが残っている場合はリークBTになります。正式BTは backtest_v6.py を使用。\n")

    conn    = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sc_conn = get_conn(db_path)

    # 月別ループ
    months = sorted(set(
        r['date'][5:7] for r in conn.execute(
            "SELECT DISTINCT date FROM results WHERE date LIKE ? AND finish<90",
            (f'{year}-%',)
        ).fetchall()
    ))

    all_races       = []    # 全レース記録
    bet_records     = []    # 賭けたレース記録（通常EV）
    yukiko_records  = []    # ゆきこ戦略: gap >= yukiko_gap
    risa_records    = []    # りさ戦略: bd_nonzero >= risa_nonzero
    combo_records   = []    # 組み合わせ: gap AND bd_nonzero両方
    lap_records     = []    # ひなた戦略: 通常EV + lap_pace_bonus >= 0

    for month_str in months:
        month = int(month_str)
        print(f"  📅 {year}年{month:02d}月 スコアリング中...", end='', flush=True)

        if month in (1, 7):
            clear_caches(full=True)
        else:
            clear_caches(full=False)

        prefetch_month(conn, year, month)

        d_from = f'{year}-{month:02d}-01'
        d_to   = f'{year}-{month:02d}-31'

        # 月内の全レース取得
        races = conn.execute("""
            SELECT DISTINCT date, venue, race_num
            FROM results WHERE date BETWEEN ? AND ? AND finish<90
            ORDER BY date, venue, race_num
        """, (d_from, d_to)).fetchall()

        month_races = 0
        month_bets  = 0

        for race in races:
            rows = conn.execute("""
                SELECT * FROM results
                WHERE date=? AND venue=? AND race_num=? AND finish<90
                ORDER BY horse_num
            """, (race['date'], race['venue'], race['race_num'])).fetchall()
            rows = [dict(r) for r in rows]

            if len(rows) < 3:
                continue
            # 障害除外
            if '障害' in str(rows[0].get('race_name', '')):
                continue

            try:
                result, meta = score_one_race(rows, sc_conn)
            except Exception as e:
                continue

            if not result:
                continue

            honmei = result[0]
            actual_win = (honmei['finish'] == 1)
            payout = get_tansho_payout(conn, meta['date'], meta['venue'],
                                       meta['race_num'], honmei['horse_num'])

            # 正解馬のランク
            winner_rank = next((h['rank'] for h in result if h['finish'] == 1), None)

            # りさ戦略用: スコアブレークダウンのnon-zero数
            bd_nonzero = honmei.get('_bd_nonzero', 0)
            gap_val    = meta.get('standout_gap', 0) or 0

            # ひなた戦略用: ラップ適性ボーナス
            lap_bonus = calc_lap_pace_bonus(
                honmei['horse_name'], meta['date'],
                meta.get('surface', ''), meta.get('recent_pci'), sc_conn
            )

            race_record = {
                **meta,
                'honmei_name':    honmei['horse_name'],
                'honmei_score':   honmei['total_score'],
                'honmei_odds':    honmei.get('odds'),
                'honmei_finish':  honmei['finish'],
                'honmei_win_prob': honmei['win_prob'],
                'honmei_ev':      honmei.get('ev'),
                'ev_ok':          honmei['ev_ok'],
                'actual_win':     actual_win,
                'payout':         payout,
                'winner_rank':    winner_rank,
                'bd_nonzero':     bd_nonzero,
                'lap_bonus':      lap_bonus,
            }
            all_races.append(race_record)
            month_races += 1

            def _make_bet(rec, payout, actual_win):
                return {
                    **rec,
                    'profit': (payout / 100 - 1) if actual_win and payout else -1,
                    'payout_actual': payout if actual_win else 0,
                }

            # 通常EV判定OK + ラップ適性フィルタ（min_lap_bonus: -0.2）
            min_lap = EV_CONDITIONS.get('min_lap_bonus', -999)
            if honmei['ev_ok'] and lap_bonus >= min_lap:
                bet_records.append(_make_bet(race_record, payout, actual_win))
                month_bets += 1

            # ゆきこ戦略: gap >= yukiko_gap (EV条件不問)
            if yukiko_gap and gap_val >= yukiko_gap and not honmei['ev_ok']:
                yukiko_records.append(_make_bet(race_record, payout, actual_win))

            # りさ戦略: bd_nonzero >= risa_nonzero (EV条件不問)
            if risa_nonzero and bd_nonzero >= risa_nonzero and not honmei['ev_ok']:
                risa_records.append(_make_bet(race_record, payout, actual_win))

            # 組み合わせ: 両方条件 (EV条件不問)
            if yukiko_gap and risa_nonzero and gap_val >= yukiko_gap and bd_nonzero >= risa_nonzero and not honmei['ev_ok']:
                combo_records.append(_make_bet(race_record, payout, actual_win))

            # ひなた戦略: 通常EV通過 + lap_bonus >= 0（適性ペース）
            if honmei['ev_ok'] and lap_bonus >= 0:
                lap_records.append(_make_bet(race_record, payout, actual_win))

        print(f" {month_races}R / 賭け{month_bets}R")

    conn.close()
    sc_conn.close()

    return all_races, bet_records, yukiko_records, risa_records, combo_records, lap_records


# ══════════════════════════════════════════════════════════════
# 集計・レポート
# ══════════════════════════════════════════════════════════════
def report(all_races, bet_records, year=YEAR):
    print(f"\n{'═'*65}")
    print(f"  ■ バックテスト結果 {year}年")
    print(f"{'═'*65}")

    total_r = len(all_races)
    if total_r == 0:
        print("  データなし")
        return

    # ── スコアリング精度 ──────────────────────────────────────
    wins_honmei = sum(1 for r in all_races if r['actual_win'])
    top3_honmei = sum(1 for r in all_races if r['winner_rank'] and r['winner_rank'] <= 3)
    top5_honmei = sum(1 for r in all_races if r['winner_rank'] and r['winner_rank'] <= 5)

    print(f"\n  ▶ スコアリング精度（全{total_r}R）")
    print(f"    ◎単勝率:  {wins_honmei}/{total_r} = {wins_honmei/total_r*100:.1f}%  (理論値約7.7%)")
    print(f"    ◎3着内率: {top3_honmei}/{total_r} = {top3_honmei/total_r*100:.1f}%  (理論値約23%)")
    print(f"    ◎5着内率: {top5_honmei}/{total_r} = {top5_honmei/total_r*100:.1f}%  (理論値約38%)")

    # 勝ち馬の平均スコアランク
    winner_ranks = [r['winner_rank'] for r in all_races if r['winner_rank']]
    if winner_ranks:
        print(f"    勝ち馬の平均スコアランク: {np.mean(winner_ranks):.2f}位 (中央{np.median(winner_ranks):.1f}位)")
        rank_dist = {i: winner_ranks.count(i) for i in range(1, 7)}
        print(f"    勝ち馬ランク分布: " + "  ".join(f"{k}位:{v}({v/total_r*100:.0f}%)" for k, v in rank_dist.items()))

    # ── EV判定レース ─────────────────────────────────────────
    n_bet = len(bet_records)
    print(f"\n  ▶ EV判定OK（賭け対象: {n_bet}R / 全体の{n_bet/total_r*100:.1f}%）")
    print(f"    条件: standout_gap≥{EV_CONDITIONS['standout_gap']} / "
          f"odds {EV_CONDITIONS['min_odds']}-{EV_CONDITIONS['max_odds']}倍 / "
          f"EV+{EV_CONDITIONS['min_ev_pct']}%以上")

    if n_bet == 0:
        print("    賭け対象レースなし")
    else:
        wins_bet = sum(1 for r in bet_records if r['actual_win'])
        total_bet_amount = n_bet * 100  # 100円×N回
        total_return     = sum(r['payout_actual'] for r in bet_records if r['payout_actual'])
        profit_total     = total_return - total_bet_amount
        roi              = total_return / total_bet_amount * 100

        avg_odds_bet = np.mean([r['honmei_odds'] for r in bet_records if r['honmei_odds']])
        avg_ev_bet   = np.mean([r['honmei_ev']   for r in bet_records if r['honmei_ev']])

        print(f"    単勝率: {wins_bet}/{n_bet} = {wins_bet/n_bet*100:.1f}%")
        print(f"    平均オッズ: {avg_odds_bet:.1f}倍 / 平均EV: {avg_ev_bet:+.1f}%")
        print(f"    ── 収支（@100円/R）──")
        print(f"    賭け総額:  {total_bet_amount:,}円")
        print(f"    払戻総額:  {total_return:,}円")
        print(f"    損益:      {profit_total:+,}円")
        print(f"    ROI:       {roi:.1f}%  ({'✅ 黒字' if profit_total > 0 else '❌ 赤字'})")

        # 月別集計
        print(f"\n  ▶ 月別収支（EV判定レースのみ）")
        from collections import defaultdict
        monthly = defaultdict(lambda: {'bets': 0, 'wins': 0, 'return': 0})
        for r in bet_records:
            ym = r['date'][:7]
            monthly[ym]['bets']   += 1
            monthly[ym]['wins']   += 1 if r['actual_win'] else 0
            monthly[ym]['return'] += r['payout_actual'] or 0
        print(f"    {'月':8s} {'賭': >4s} {'勝': >4s} {'勝率': >6s} {'収支': >8s} {'ROI': >7s}")
        for ym in sorted(monthly.keys()):
            m = monthly[ym]
            b_amt = m['bets'] * 100
            profit = m['return'] - b_amt
            roi_m  = m['return'] / b_amt * 100 if b_amt > 0 else 0
            mark   = '✅' if profit > 0 else '❌'
            print(f"    {ym}  {m['bets']:4d}  {m['wins']:4d}  {m['wins']/m['bets']*100:5.1f}%  {profit:+7,}円  {roi_m:6.1f}% {mark}")

    # ── 会場別精度 ───────────────────────────────────────────
    print(f"\n  ▶ 会場別 ◎単勝率（上位/下位）")
    from collections import defaultdict
    venue_stats = defaultdict(lambda: {'total': 0, 'wins': 0})
    for r in all_races:
        venue_stats[r['venue']]['total'] += 1
        if r['actual_win']:
            venue_stats[r['venue']]['wins'] += 1
    venue_list = [(v, d['wins']/d['total']*100, d['total'])
                  for v, d in venue_stats.items() if d['total'] >= 10]
    venue_list.sort(key=lambda x: -x[1])
    for v, wr, n in venue_list:
        bar = '█' * int(wr / 2)
        print(f"    {v:4s} {bar:20s} {wr:5.1f}% ({n}R)")

    # ── standout_gap 別精度 ──────────────────────────────────
    print(f"\n  ▶ standout_gap 別 ◎単勝率")
    gap_bins = [(0, 5), (5, 8), (8, 12), (12, 20), (20, 999)]
    for lo, hi in gap_bins:
        subset = [r for r in all_races if lo <= r['standout_gap'] < hi]
        if not subset:
            continue
        wr = sum(1 for r in subset if r['actual_win']) / len(subset) * 100
        print(f"    gap {lo:2d}-{hi:3d}: {wr:5.1f}%  ({len(subset)}R)")

    print(f"\n{'═'*65}\n")

    # JSON保存
    out = {
        'year': year,
        'total_races': total_r,
        'honmei_win_rate': round(wins_honmei / total_r * 100, 2),
        'ev_bets': n_bet,
        'ev_win_rate': round(wins_bet / n_bet * 100, 2) if n_bet > 0 else None,
        'roi': round(roi, 2) if n_bet > 0 else None,
        'profit': profit_total if n_bet > 0 else None,
        'bet_records': [
            {k: v for k, v in r.items() if k not in ['_blood_score', '_sire', '_blood_rank']}
            for r in bet_records
        ],
    }
    fname = f'backtest_{year}.json'
    with open(fname, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"  💾 詳細結果: {fname}")
    return out


# ══════════════════════════════════════════════════════════════
# エントリポイント
# ══════════════════════════════════════════════════════════════
def report_strategy(label, records, bet_amount=1000):
    """戦略別レポートを出力"""
    if not records:
        print(f"  {label:30} 0戦 (該当なし)")
        return
    n = len(records)
    wins = sum(1 for r in records if r.get('actual_win'))
    ret = sum(r.get('payout_actual', 0) for r in records)
    cost = n * bet_amount
    roi = ret / cost * 100 if cost else 0
    profit = ret - cost
    win_rate = wins / n * 100
    print(f"  {label:30} {n:3}戦 {wins:2}勝 勝率{win_rate:4.1f}%  収支{profit:+7,}円  ROI={roi:6.1f}%")
    # 月別
    from collections import defaultdict
    monthly = defaultdict(lambda: {'n':0,'w':0,'ret':0})
    for r in records:
        m = r['date'][:7]
        monthly[m]['n']+=1
        monthly[m]['w']+=1 if r.get('actual_win') else 0
        monthly[m]['ret']+=r.get('payout_actual',0)
    for m in sorted(monthly.keys()):
        s=monthly[m]; roi_m=s['ret']/(s['n']*bet_amount)*100 if s['n'] else 0
        mark='✅' if roi_m>=100 else '❌'
        print(f"    {m}  {s['n']:3}戦 {s['w']:2}勝  ROI={roi_m:6.1f}% {mark}")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--year', type=int, default=YEAR)
    parser.add_argument('--yukiko-gap', type=float, default=12.0,
                        help='ゆきこ戦略: gap>=この値のレースをEV不問で買う (default=12)')
    parser.add_argument('--risa-nonzero', type=int, default=3,
                        help='りさ戦略: bd_nonzero>=この値のレースをEV不問で買う (default=3)')
    args = parser.parse_args()

    all_races, bet_records, yukiko_recs, risa_recs, combo_recs, lap_recs = run_backtest(
        year=args.year,
        yukiko_gap=args.yukiko_gap,
        risa_nonzero=args.risa_nonzero,
    )
    report(all_races, bet_records, year=args.year)

    # ── 追加戦略比較レポート ─────────────────────────────────
    print(f"\n{'═'*65}")
    print(f"  委員会戦略 比較レポート ({args.year}年)")
    print(f"  ゆきこ: gap>={args.yukiko_gap}  /  りさ: bd>={args.risa_nonzero}  /  ひなた: lap_bonus>=0")
    print(f"{'═'*65}")
    report_strategy("v6.6通常(EV判定)",                   bet_records,  bet_amount=1000)
    report_strategy(f"ゆきこ(gap>={args.yukiko_gap}, EV不問)", yukiko_recs, bet_amount=1000)
    report_strategy(f"りさ(bd>={args.risa_nonzero}, EV不問)",  risa_recs,   bet_amount=1000)
    report_strategy("組み合わせ(gap+bd両方)",              combo_recs,   bet_amount=1000)
    report_strategy("ひなた(EV通常+lap適性>=0)",           lap_recs,     bet_amount=1000)
    print(f"{'═'*65}\n")
