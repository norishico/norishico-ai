"""今週末のレースにスコアリングを実行して予想HTML生成"""
import sqlite3, json, sys, time
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, '.')
import importlib, scoring
importlib.reload(scoring)

from scoring import (get_conn, score_past_performance, score_course_fitness,
    score_jockey_trainer, score_rotation, score_training_actual, score_bloodline,
    score_gate_style, get_weights, calc_pace_context, _infer_running_style,
    calc_course_blood_bonus, calc_gate_cond_blood_bonus, calc_track_bias_bonus,
    calc_venue_sire_bonus, calc_venue_damsire_bonus, EV_CONDITIONS,
    _past_runs_cache, _course_runs_cache, _running_style_cache,
    _jockey_cache, _trainer_cache, _combo_cache, _ace_cache,
    _avg_time_cache, _last3f_cache, _training_actual_cache,
    _bloodline_score_cache, _week_cache, _wet_perf_cache)
from backtest_2026 import prefetch_month, clear_caches
from backtest_full import prefetch_score_caches, prefetch_jt, grade_full
from backtest_v2 import calc_win_prob_s12, calc_ev_scale7
from backtest_v6 import is_buy_v6, is_special_buy, SUNDAY_SIRES
import numpy as np

DB_PATH = 'keiba.db'

# JRA重賞名→グレード辞書（主要重賞）
GRADED_RACES = {
    # G1
    'フェブラリーS': 'G1', 'フェブラリーステークス': 'G1',
    '高松宮記念': 'G1', '大阪杯': 'G1', '桜花賞': 'G1', '皐月賞': 'G1',
    '天皇賞': 'G1', 'NHKマイルC': 'G1', 'NHKマイルカップ': 'G1',
    'ヴィクトリアマイル': 'G1', 'オークス': 'G1', '優駿牝馬': 'G1',
    'ダービー': 'G1', '日本ダービー': 'G1', '東京優駿': 'G1',
    '安田記念': 'G1', '宝塚記念': 'G1', 'スプリンターズS': 'G1',
    '秋華賞': 'G1', '菊花賞': 'G1', 'マイルCS': 'G1', 'マイルチャンピオンシップ': 'G1',
    'エリザベス女王杯': 'G1', 'ジャパンC': 'G1', 'ジャパンカップ': 'G1',
    'チャンピオンズC': 'G1', '阪神JF': 'G1', '阪神ジュベナイルF': 'G1',
    '朝日杯FS': 'G1', '朝日杯フューチュリティS': 'G1',
    'ホープフルS': 'G1', 'ホープフルステークス': 'G1',
    '有馬記念': 'G1',
    # G2
    '日経新春杯': 'G2', 'AJCC': 'G2', 'アメリカJCC': 'G2',
    '京都記念': 'G2', '中山記念': 'G2', '阪急杯': 'G2',
    'チューリップ賞': 'G2', '弥生賞': 'G2', 'フィリーズレビュー': 'G2',
    '金鯱賞': 'G2', 'スプリングS': 'G2', '阪神大賞典': 'G2',
    '毎日杯': 'G2', '産経大阪杯': 'G2', 'ニュージーランドT': 'G2',
    '青葉賞': 'G2', '京王杯SC': 'G2', '目黒記念': 'G2',
    '札幌記念': 'G2', '新潟記念': 'G2', 'セントウルS': 'G2',
    'ローズS': 'G2', 'オールカマー': 'G2', '神戸新聞杯': 'G2',
    '毎日王冠': 'G2', '府中牝馬S': 'G2', '京都大賞典': 'G2',
    '富士S': 'G2', 'スワンS': 'G2', 'アルゼンチン共和国杯': 'G2',
    'ステイヤーズS': 'G2', '阪神C': 'G2', '中日新聞杯': 'G2',
    '日経賞': 'G2',
    # G3（主要なもの）
    'ダービー卿CT': 'G3', 'ダービー卿チャレンジトロフィー': 'G3',
    'チャーチルダウンズC': 'G3',
    'フローラS': 'G3', 'フローラステークス': 'G3',
    'アーリントンC': 'G3', 'アンタレスS': 'G3',
    '福島牝馬S': 'G3', '小倉大賞典': 'G3', '京都牝馬S': 'G3',
    'シルクロードS': 'G3', '東京新聞杯': 'G3', 'きさらぎ賞': 'G3',
    '共同通信杯': 'G3', 'ダイヤモンドS': 'G3', '京都記念': 'G2',
    '中山牝馬S': 'G3', 'ファルコンS': 'G3', 'フラワーC': 'G3',
    '高松宮記念': 'G1', 'マーチS': 'G3', '阪神牝馬S': 'G3',
    # ポラリスS, バイオレットS はリステッド(L)/OP特別のため除外
}

def grade_for_prediction(race):
    """netkeibaのレースデータからグレードを判定"""
    rname = race.get('race_name', '')
    data2 = race.get('race_data2', '')

    # 1. 重賞名辞書で判定（長い名前から先にマッチさせる）
    for name, grade in sorted(GRADED_RACES.items(), key=lambda x: -len(x[0])):
        if name in rname:
            return grade

    # 2. 賞金からG1/G2/G3を推定（data2にある場合）
    import re
    prize_match = re.search(r'本賞金:(\d+)', data2)
    if prize_match:
        prize = int(prize_match.group(1))
        if prize >= 10000: return 'G1'   # 1億以上
        if prize >= 5000:  return 'G2'   # 5000万以上
        if prize >= 3500:  return 'G3'   # 3500万以上

    # 3. data2のクラス表記で判定
    if 'オープン' in data2:
        # 賞金がなくてオープンなら重賞の可能性
        # ただしOP特別もあるのでG3ではなくオープン扱い
        return '3勝'  # OP特別は3勝と同等

    # 4. 通常のgrade_fullにフォールバック
    return grade_full(rname)


def score_weekend_race(race, conn, sc_conn):
    """1レースのスコアリング"""
    horses = race.get('horses', [])
    if len(horses) < 3: return None

    venue = race.get('venue', '')
    surf_raw = race.get('surface', '芝')
    surface = '芝' if '芝' in str(surf_raw) else 'ダ'
    dist = race.get('distance', 1600)
    cond = race.get('track_cond', '良') or '良'
    rname = race.get('race_name', '')
    date = '2026-04-04'  # 仮
    if '04' in str(race.get('race_id',''))[-4:-2]:
        date = '2026-04-05'
    heads = len(horses)
    gr = grade_for_prediction(race)
    W = get_weights(gr)

    # 展開コンテキスト
    race_styles = []
    for h in horses:
        style = _infer_running_style(h['name'], date, surface, dist, sc_conn)
        race_styles.append(style)

    pci_row = sc_conn.execute("""
        SELECT AVG(pci) as avg_pci FROM (
            SELECT pci FROM race_pace
            WHERE venue=? AND surface=? AND distance=? AND date < ? AND pci IS NOT NULL
            ORDER BY date DESC LIMIT 5)
    """, (venue, surface, dist, date)).fetchone()
    recent_pci = float(pci_row['avg_pci']) if pci_row and pci_row['avg_pci'] else None
    pace_ctx = calc_pace_context(race_styles, recent_pci=recent_pci)
    pace_mult = pace_ctx['mult']

    results = []
    for h in horses:
        name = h['name']
        jockey = h.get('jockey', '')
        trainer = h.get('trainer', '').replace('栗東','').replace('美浦','').strip()
        odds = float(h.get('odds', '0') or '0')
        pop = int(h.get('popularity', '0') or '0')

        # DBから血統情報を取得
        db_horse = conn.execute("""
            SELECT sire, dam_sire, horse_weight, prev_finish, prev_distance,
                   prev_venue, prev_surface, umaban
            FROM results WHERE TRIM(horse_name) = ? AND finish < 90
            ORDER BY date DESC LIMIT 1
        """, (name,)).fetchone()

        sire = db_horse['sire'].strip() if db_horse and db_horse['sire'] else ''
        dam_sire = db_horse['dam_sire'].strip() if db_horse and db_horse['dam_sire'] else ''
        hw = int(db_horse['horse_weight']) if db_horse and db_horse['horse_weight'] and db_horse['horse_weight'] > 0 else 0
        prev_fin = db_horse['prev_finish'] if db_horse else None
        prev_dist = db_horse['prev_distance'] if db_horse else None

        ym = date[:7]
        prev_runs = _past_runs_cache.get((name, ym), [])

        interval_weeks = None
        if prev_runs:
            last_d = prev_runs[0].get('date')
            if last_d:
                try:
                    interval_weeks = (datetime.strptime(date, '%Y-%m-%d') -
                                      datetime.strptime(last_d, '%Y-%m-%d')).days / 7.0
                except: pass

        wc = None
        if hw > 0 and prev_runs and prev_runs[0].get('horse_weight', 0) > 0:
            wc = hw - prev_runs[0]['horse_weight']

        hn = h.get('umaban', 0) or (len(results) + 1)  # 枠確定前はインデックス

        sp  = score_past_performance(name, date, surface, dist, sc_conn)
        sc2 = score_course_fitness(name, date, surface, dist, cond, sc_conn, venue)
        sj  = score_jockey_trainer(jockey, trainer, date, surface, dist, sc_conn, name, grade=gr)
        sr  = score_rotation(interval_weeks, prev_fin, prev_dist, dist, gr,
                             horse_weight=hw, surface=surface, weight_change=wc)
        sb  = score_bloodline(sire, dam_sire, date, surface, dist, sc_conn)
        st  = score_training_actual(name, date, sc_conn)
        sg  = score_gate_style(name, hn, date, venue, surface, dist, sc_conn, pace_mult)

        total = (
            sp['score'] * W['past_performance'] +
            sc2['score'] * W['course_fitness'] +
            sj['score'] * W['jockey_trainer'] +
            sr['score'] * W['rotation'] +
            st['score'] * W['training'] +
            sb['score'] * W['sire'] +
            sb['dam_sire'] * W['dam_sire'] +
            sg['score'] * W['gate_style']
        )

        results.append({
            'horse_name': name, 'horse_num': hn, 'jockey': jockey,
            'odds': odds, 'popularity': pop, 'total_score': round(total, 1),
            '_blood_score': sb['score'], '_sire': sire, '_dam_sire': dam_sire,
            '_prev_pos4': int(prev_runs[0]['pos4']) if prev_runs and prev_runs[0].get('pos4') else 0,
            'accel_lap': st.get('accel_lap', False),
            'has_good_train': st.get('has_good_train', False),
            'trainer': trainer, 'waku': h.get('waku', 0),
        })

    results.sort(key=lambda x: x['total_score'], reverse=True)

    # 血統ランク → 相乗ボーナス
    blood_sorted = sorted(results, key=lambda x: x['_blood_score'], reverse=True)
    for rank, h2 in enumerate(blood_sorted, 1):
        h2['_blood_rank'] = rank

    for h2 in results:
        bonus = calc_course_blood_bonus(h2['horse_name'], date, venue, surface, dist,
                                        h2['_blood_rank'], sc_conn)
        gcbb  = calc_gate_cond_blood_bonus(h2['horse_name'], date, venue, surface, dist,
                                           h2['horse_num'], heads, cond, h2['_sire'], sc_conn)
        tbb   = calc_track_bias_bonus(venue, surface, date, h2['horse_num'], heads,
                                       h2['_prev_pos4'], sc_conn)
        vsb   = calc_venue_sire_bonus(venue, dist, h2['_sire'], sc_conn)
        vdsb  = calc_venue_damsire_bonus(venue, dist, h2['_dam_sire'], sc_conn)
        h2['total_score'] = round(h2['total_score'] + bonus + gcbb + tbb + vsb + vdsb, 1)

    results.sort(key=lambda x: x['total_score'], reverse=True)
    for rank, h2 in enumerate(results, 1):
        h2['rank'] = rank

    # EV計算
    scores = np.array([h2['total_score'] for h2 in results])
    exp_s = np.exp((scores - scores.mean()) / 10)
    probs = exp_s / exp_s.sum()
    gap = round(results[0]['total_score'] - results[1]['total_score'], 1) if len(results) > 1 else 0

    honmei = results[0]
    ni = results[1] if len(results) > 1 else None
    honmei_odds = honmei.get('odds', 0) or 0
    ev7 = calc_ev_scale7(scores.tolist(), honmei_odds)

    # 買い判定
    buy_type = None
    honmei_good = honmei.get('has_good_train', False)
    honmei_sire = honmei.get('_sire', '')
    buy_zone, ev_ok = is_buy_v6(gr, heads, gap, honmei_odds, ev7, good_train=honmei_good, sire=honmei_sire, track_cond=cond)
    if ev_ok:
        if buy_zone == 'challenge':
            buy_type = 'v6_challenge'
        elif gap >= 10 and honmei_good and (calc_venue_sire_bonus(venue, dist, honmei_sire, sc_conn) > 0):
            buy_type = 'v6_star3'
        else:
            buy_type = 'v6_star2'

    # 別枠C2/F1判定（全馬チェック）
    special_horse = None
    for h2 in results:
        sp_buy, sp_rule = is_special_buy(
            gr, h2.get('odds', 0) or 0, h2.get('popularity', 0),
            heads, h2.get('accel_lap', False), h2.get('has_good_train', False),
            h2.get('_sire', ''))
        if sp_buy:
            special_horse = {**h2, 'rule': sp_rule}
            break

    # 根拠タグ
    reasons = []
    if honmei_good: reasons.append('調教好仕上がり')
    if gap >= 8: reasons.append('スコア突出')
    if calc_venue_sire_bonus(venue, dist, honmei_sire, sc_conn) > 0: reasons.append('コース適性◎')
    if calc_venue_damsire_bonus(venue, dist, honmei.get('_dam_sire',''), sc_conn) > 0: reasons.append('母父適性◎')
    if honmei_odds >= 10: reasons.append('配当妙味◎')
    if not reasons: reasons.append('AI総合評価')

    return {
        'race': race, 'grade': gr, 'results': results, 'gap': gap, 'ev7': ev7,
        'buy_type': buy_type, 'special_horse': special_horse, 'reasons': reasons,
        'honmei': honmei, 'ni': ni, 'heads': heads,
    }


def main():
    print("Loading race data...")
    races = json.load(open('this_week_races.json', encoding='utf-8'))
    print(f"  {len(races)} races loaded")

    print("Initializing DB...")
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")
    sc_conn = sqlite3.connect(DB_PATH); sc_conn.row_factory = sqlite3.Row
    sc_conn.execute("PRAGMA cache_size=-65536")
    sc_conn.execute("PRAGMA temp_store=MEMORY")

    print("Prefetching caches...")
    from build_supplementary_tables import (
        build_bloodline_stats, build_gate_cond_blood_bonus, build_track_bias_bonus
    )
    cutoff = '2026-04-01'
    build_bloodline_stats(sc_conn, cutoff_date=cutoff)
    build_gate_cond_blood_bonus(sc_conn, cutoff_date=cutoff)
    build_track_bias_bonus(sc_conn, cutoff_date=cutoff)
    scoring._bloodline_score_cache.clear()
    scoring._gcbb_cache.clear(); scoring._gcbb_loaded = False
    scoring._tbb_cache.clear(); scoring._week_cache.clear()
    clear_caches(full=True)
    prefetch_score_caches(sc_conn, cutoff_date=cutoff)
    prefetch_month(conn, 2026, 3)  # 直近月のデータをプリフェッチ
    prefetch_month(conn, 2026, 4)
    prefetch_jt(conn, 2026, 4)

    print("\nScoring races...")
    predictions = []
    for r in races:
        try:
            pred = score_weekend_race(r, conn, sc_conn)
            if pred:
                predictions.append(pred)
                bt = pred['buy_type'] or ''
                sp = pred['special_horse']
                rr = pred['race']
                mark = ''
                if bt: mark = f' ★BUY({bt})'
                if sp: mark += f' ◆{sp["rule"]}'
                print(f"  {rr.get('venue','')}{rr.get('race_num',0):>2}R {rr.get('race_name',''):>15} "
                      f"◎{pred['honmei']['horse_name']:>12} {pred['honmei']['total_score']:>5.1f}pt "
                      f"gap={pred['gap']:>4.1f}{mark}")
        except Exception as e:
            print(f"  Error: {r.get('venue','')}{r.get('race_num',0)}R - {e}")

    conn.close(); sc_conn.close()

    # JSON保存
    # predictions内のnumpy型を変換
    def convert(obj):
        if isinstance(obj, (np.integer,)): return int(obj)
        if isinstance(obj, (np.floating,)): return float(obj)
        if isinstance(obj, np.ndarray): return obj.tolist()
        return str(obj)

    with open('weekend_predictions.json', 'w', encoding='utf-8') as f:
        json.dump(predictions, f, ensure_ascii=False, indent=2, default=convert)

    # 集計
    buy_count = sum(1 for p in predictions if p['buy_type'])
    sp_count = sum(1 for p in predictions if p['special_horse'])
    print(f"\n=== 予想結果 ===")
    print(f"  全{len(predictions)}R中 買い推奨{buy_count}R + 別枠{sp_count}R")
    print(f"  → weekend_predictions.json に保存")


if __name__ == '__main__':
    main()
