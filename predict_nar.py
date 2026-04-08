"""NAR交流重賞の簡易予想ツール

JRA馬のスコアリングデータを活用してランキングを出力。
NAR専用馬はデフォルトスコア50。買い推奨は行わない。

Usage:
  python predict_nar.py --url "https://nar.netkeiba.com/race/shutuba.html?race_id=..."
  python predict_nar.py --horses "ディクテオン,アウトレンジ,テンカジョウ" --surface ダ --distance 2100 --date 2026-04-08
"""
import sqlite3, json, sys, argparse, re, time
from datetime import datetime

sys.path.insert(0, '.')
sys.stdout.reconfigure(encoding='utf-8')

import importlib, scoring
importlib.reload(scoring)

from scoring import (get_conn, score_past_performance, score_course_fitness,
    score_jockey_trainer, score_rotation, score_training_actual, score_bloodline,
    score_gate_style, get_weights, calc_pace_context, _infer_running_style,
    score_surface_switch, _surface_switch_cache,
    _past_runs_cache, _course_runs_cache, _running_style_cache,
    _jockey_cache, _trainer_cache, _combo_cache, _ace_cache,
    _avg_time_cache, _last3f_cache, _training_actual_cache,
    _bloodline_score_cache, _week_cache, _wet_perf_cache)
from backtest_2026 import prefetch_month, clear_caches
from backtest_full import prefetch_score_caches, prefetch_jt
import numpy as np

DB_PATH = 'keiba.db'

# NAR重賞グレード辞書
NAR_GRADED = {
    '帝王賞': 'G1', '東京大賞典': 'G1', 'JBCクラシック': 'G1',
    'JBCスプリント': 'G1', 'JBCレディスクラシック': 'G1',
    '川崎記念': 'G1', 'かしわ記念': 'G1',
    'マイルチャンピオンシップ南部杯': 'G1', '全日本2歳優駿': 'G1',
    'ダイオライト記念': 'G2', '浦和記念': 'G2', '佐賀記念': 'G3',
    'レディスプレリュード': 'G2', '関東オークス': 'G2',
    '東京スプリント': 'G3', 'さきたま杯': 'G2',
    'エンプレス杯': 'G2', 'クイーン賞': 'G3',
}


def detect_grade(race_name):
    """レース名からグレードを推定"""
    for key, grade in NAR_GRADED.items():
        if key in race_name:
            return grade
    if 'Jpn1' in race_name or 'JpnI' in race_name or 'JpnⅠ' in race_name:
        return 'G1'
    if 'Jpn2' in race_name or 'JpnII' in race_name or 'JpnⅡ' in race_name:
        return 'G2'
    if 'Jpn3' in race_name or 'JpnIII' in race_name or 'JpnⅢ' in race_name:
        return 'G3'
    return 'OP'


def lookup_jra_horse(name, conn):
    """keiba.dbでJRA出走歴を検索"""
    row = conn.execute(
        "SELECT sire, dam_sire, horse_weight, prev_finish, prev_distance, "
        "prev_venue, prev_surface FROM results "
        "WHERE TRIM(horse_name)=? AND finish<90 ORDER BY date DESC LIMIT 1",
        (name.strip(),)
    ).fetchone()
    return dict(row) if row else None


def score_nar_race(horses, race_info, conn, sc_conn):
    """NAR交流重賞のスコアリング

    Args:
        horses: [{'name': '馬名', 'jockey': '騎手', 'trainer': '調教師',
                  'umaban': 1, 'odds': 5.0}, ...]
        race_info: {'race_name': '川崎記念', 'venue': '川崎', 'surface': 'ダ',
                    'distance': 2100, 'date': '2026-04-08', 'track_cond': '良'}
    """
    venue = race_info.get('venue', '')
    surface = race_info.get('surface', 'ダ')
    dist = race_info.get('distance', 2100)
    date = race_info.get('date', datetime.now().strftime('%Y-%m-%d'))
    cond = race_info.get('track_cond', '良')
    race_name = race_info.get('race_name', '')
    gr = detect_grade(race_name)
    W = get_weights(gr)
    heads = len(horses)

    # プリフェッチ（prefetch_monthのみ。score_cachesはテーブル書き込みが発生するためスキップ。
    # scoring関数はキャッシュミス時にDB直接クエリで動作する）
    ym = date[:7]
    year = int(date[:4])
    month = int(date[5:7])
    clear_caches()
    try:
        prefetch_month(conn, year, month)
    except Exception:
        pass  # キャッシュなしでも動作する

    # 展開コンテキスト（JRA馬のみ）
    race_styles = []
    for h in horses:
        style = _infer_running_style(h['name'].strip(), date, surface, dist, sc_conn)
        race_styles.append(style)
    pace_ctx = calc_pace_context(race_styles, recent_pci=None)
    pace_mult = pace_ctx['mult']

    results = []
    for h in horses:
        name = h['name'].strip()
        jockey = h.get('jockey', '')
        trainer = h.get('trainer', '').replace('栗東', '').replace('美浦', '').strip()
        odds = float(h.get('odds', 0) or 0)
        umaban = int(h.get('umaban', 0) or 0)

        # JRA馬かどうか判定
        db_horse = lookup_jra_horse(name, conn)
        is_jra = db_horse is not None

        if not is_jra:
            # NAR専用馬: デフォルトスコア
            results.append({
                'horse_name': name, 'umaban': umaban, 'jockey': jockey,
                'odds': odds, 'total_score': 50.0, 'is_jra': False,
                'detail': 'JRAデータなし',
                'scores': {k: 50.0 for k in ['past', 'course', 'jt', 'rotation',
                                               'training', 'sire', 'dam_sire', 'gate_style']},
            })
            continue

        # JRA馬: フルスコアリング
        sire = (db_horse.get('sire') or '').strip()
        dam_sire = (db_horse.get('dam_sire') or '').strip()
        hw = int(db_horse['horse_weight']) if db_horse.get('horse_weight') and db_horse['horse_weight'] > 0 else 0
        prev_fin = db_horse.get('prev_finish')
        prev_dist = db_horse.get('prev_distance')

        prev_runs = _past_runs_cache.get((name, ym), [])
        interval_weeks = None
        if prev_runs:
            last_d = prev_runs[0].get('date')
            if last_d:
                try:
                    interval_weeks = (datetime.strptime(date, '%Y-%m-%d') -
                                      datetime.strptime(last_d, '%Y-%m-%d')).days / 7.0
                except:
                    pass

        wc = None
        if hw > 0 and prev_runs and prev_runs[0].get('horse_weight', 0) > 0:
            wc = hw - prev_runs[0]['horse_weight']

        sp = score_past_performance(name, date, surface, dist, sc_conn)
        sc2 = score_course_fitness(name, date, surface, dist, cond, sc_conn, venue)
        sj = score_jockey_trainer(jockey, trainer, date, surface, dist, sc_conn, name, grade=gr)
        sr = score_rotation(interval_weeks, prev_fin, prev_dist, dist, gr,
                            horse_weight=hw, surface=surface, weight_change=wc)
        sb = score_bloodline(sire, dam_sire, date, surface, dist, sc_conn)
        st = score_training_actual(name, date, sc_conn)
        sg = score_gate_style(name, umaban, date, venue, surface, dist, sc_conn, pace_mult)

        # 初ダート/初芝 転向補正
        s_switch = score_surface_switch(name, date, surface, dist, sc_conn,
                                        sire=sire, dam_sire=dam_sire)
        if s_switch:
            sp['score'] = max(0, min(100, sp['score'] + s_switch['past_adj']))
            sc2['score'] = max(0, min(100, sc2['score'] + s_switch['course_adj']))

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
            'horse_name': name, 'umaban': umaban, 'jockey': jockey,
            'odds': odds, 'total_score': round(total, 1), 'is_jra': True,
            'sire': sire, 'dam_sire': dam_sire,
            'surface_switch': s_switch['detail'] if s_switch else '',
            'detail': f"past={sp['score']:.0f} course={sc2['score']:.0f} "
                      f"JT={sj['score']:.0f} train={st['score']:.0f} "
                      f"blood={sb['score']:.0f}/{sb['dam_sire']:.0f} "
                      f"gate={sg['score']:.0f}",
            'scores': {
                'past': sp['score'], 'course': sc2['score'],
                'jt': sj['score'], 'rotation': sr['score'],
                'training': st['score'], 'sire': sb['score'],
                'dam_sire': sb['dam_sire'], 'gate_style': sg['score'],
            },
        })

    results.sort(key=lambda x: x['total_score'], reverse=True)

    # 印付け
    for i, h in enumerate(results):
        if i == 0:   h['mark'] = '◎'
        elif i == 1: h['mark'] = '○'
        elif i == 2: h['mark'] = '▲'
        elif i <= 4: h['mark'] = '△'
        else:        h['mark'] = '—'

    return results


def print_results(race_info, results):
    """テキスト形式で予想出力"""
    print()
    print(f"{'='*60}")
    rn = race_info.get('race_name', '?')
    gr = detect_grade(rn)
    print(f"  {rn} ({gr}) {race_info.get('venue','')} "
          f"{race_info.get('surface','ダ')}{race_info.get('distance',0)}m")
    print(f"  {race_info.get('date','')}  馬場: {race_info.get('track_cond','良')}")
    print(f"{'='*60}")
    print(f"  印  馬番  馬名{'':12s} スコア  JRA  odds    スコア内訳")
    print(f"  {'─'*56}")

    for h in results:
        jra = '○' if h['is_jra'] else '×'
        odds_s = f"{h['odds']:5.1f}" if h['odds'] else '  ---'
        sw = f" [{h['surface_switch']}]" if h.get('surface_switch') else ''
        print(f"  {h['mark']}  {h['umaban']:>3d}  {h['horse_name']:14s} "
              f"{h['total_score']:5.1f}   {jra}  {odds_s}  {h['detail']}{sw}")

    print()
    print("  ※ NAR交流重賞の参考予想（JRAデータのみ）。買い推奨は行いません。")
    print(f"  ※ JRA馬: {sum(1 for h in results if h['is_jra'])}頭 / "
          f"NAR馬: {sum(1 for h in results if not h['is_jra'])}頭")
    print()


def main():
    parser = argparse.ArgumentParser(description='NAR交流重賞 簡易予想ツール')
    parser.add_argument('--url', help='NAR netkeiba 出馬表URL')
    parser.add_argument('--horses', help='馬名カンマ区切り（手動入力）')
    parser.add_argument('--surface', default='ダ', help='芝 or ダ (default: ダ)')
    parser.add_argument('--distance', type=int, default=2100, help='距離 (default: 2100)')
    parser.add_argument('--date', default=None, help='レース日 YYYY-MM-DD')
    parser.add_argument('--venue', default='', help='会場名')
    parser.add_argument('--race-name', default='交流重賞', help='レース名')
    parser.add_argument('--cond', default='良', help='馬場状態')
    parser.add_argument('--json', help='出走表JSONファイル')
    args = parser.parse_args()

    date = args.date or datetime.now().strftime('%Y-%m-%d')

    if args.horses:
        # 手動入力モード
        names = [n.strip() for n in args.horses.split(',')]
        horses = [{'name': n, 'jockey': '', 'trainer': '', 'umaban': i+1, 'odds': 0}
                  for i, n in enumerate(names)]
        race_info = {
            'race_name': args.race_name, 'venue': args.venue,
            'surface': args.surface, 'distance': args.distance,
            'date': date, 'track_cond': args.cond,
        }
    elif args.json:
        with open(args.json, encoding='utf-8') as f:
            data = json.load(f)
        horses = data.get('horses', [])
        race_info = {k: data[k] for k in ['race_name', 'venue', 'surface', 'distance',
                                            'date', 'track_cond'] if k in data}
    elif args.url:
        print("URL取得モードは未実装です。--horses で手動入力してください。")
        sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)

    conn = get_conn(DB_PATH)
    sc_conn = get_conn(DB_PATH)

    results = score_nar_race(horses, race_info, conn, sc_conn)
    print_results(race_info, results)

    conn.close()
    sc_conn.close()


if __name__ == '__main__':
    main()
