"""backtest_nar.py - 南関東競馬 バックテスト
使い方:
  python nar/backtest_nar.py                         # 全期間
  python nar/backtest_nar.py --year 2025
  python nar/backtest_nar.py --since 2024-01-01 --until 2024-12-31
  python nar/backtest_nar.py --breakdown             # 会場/クラス別集計
  python nar/backtest_nar.py --walkforward           # 年次Walk-Forward
"""
import sys, sqlite3, argparse, json
from pathlib import Path
from datetime import date
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

PROJ = Path(__file__).parent.parent
DB_PATH = PROJ / 'nar_keiba.db'

sys.path.insert(0, str(Path(__file__).parent))
import scoring_nar
from scoring_nar import score_horse, pick_honmei, CLASS_RANK, get_db
import statistics


WINSORIZE_CAP = 50_000  # 5万円キャップ


def winsorize(v):
    return min(v, WINSORIZE_CAP)


def _build_trailing_time_index(conn, before_date, min_n=20):
    """before_date より前のレースだけを使って (venue,distance,condition)->(avg_t,std_t,avg_l3f,std_l3f) を構築。
    WF-CVの各年foldでリークなく上がり3Fスコアを測るための時系列版time_index。
    ライブ予想時のscoring_nar._load_time_index(全期間版、正しい挙動)は変更しない。"""
    rows = conn.execute("""
        SELECT rc.venue_name, rc.distance, COALESCE(rc.condition, '良') as cond,
               r.time_sec, r.last_3f
        FROM nar_results r JOIN nar_races rc ON r.race_id = rc.race_id
        WHERE r.time_sec > 0 AND rc.date < ?
    """, (before_date,)).fetchall()

    t_groups = defaultdict(list)
    l3f_groups = defaultdict(list)
    for venue, dist, cond, t, l3f in rows:
        key = (venue, dist, cond)
        t_groups[key].append(t)
        if l3f and l3f > 0:
            l3f_groups[key].append(l3f)

    time_index = {}
    for key, times in t_groups.items():
        if len(times) < min_n:
            continue
        avg_t = sum(times) / len(times)
        std_t = statistics.stdev(times) if len(times) > 1 else 1.0
        l3fs = l3f_groups.get(key, [])
        avg_l3f = sum(l3fs) / len(l3fs) if l3fs else None
        std_l3f = statistics.stdev(l3fs) if len(l3fs) > 1 else None
        time_index[key] = (round(avg_t, 3), round(std_t, 3),
                            round(avg_l3f, 3) if avg_l3f else None,
                            round(std_l3f, 3) if std_l3f else None)
    return time_index


_pick_min_score = None
_pick_min_odds = None
_pick_max_odds = None
_pick_gap_req = None
_pick_max_gap_req = None


def run_backtest(conn, since='2022-01-01', until='2099-12-31', verbose=False, venue=None):
    if venue:
        races = conn.execute("""
            SELECT race_id, date, venue_cd, venue_name, race_num, class_code,
                   distance, track_type, direction, condition, heads_count
            FROM nar_races
            WHERE date >= ? AND date <= ? AND venue_name = ?
            ORDER BY date, race_num
        """, (since, until, venue)).fetchall()
    else:
        races = conn.execute("""
            SELECT race_id, date, venue_cd, venue_name, race_num, class_code,
                   distance, track_type, direction, condition, heads_count
            FROM nar_races
            WHERE date >= ? AND date <= ?
            ORDER BY date, race_num
        """, (since, until)).fetchall()

    total_cost = total_ret = total_ret_w = 0
    wins = 0
    records = []

    for race_row in races:
        (race_id, d, venue_cd, venue_name, race_num, class_code,
         distance, track_type, direction, condition, heads_count) = race_row

        entries = conn.execute("""
            SELECT umaban, horse_name, jockey, odds, popularity, finish, stable, body_weight_diff
            FROM nar_results
            WHERE race_id = ?
            ORDER BY umaban
        """, (race_id,)).fetchall()

        if not entries:
            continue

        race_info = {
            'race_id': race_id,
            'date': d,
            'venue_cd': venue_cd,
            'venue_name': venue_name,
            'race_num': race_num,
            'class_code': class_code,
            'distance': distance,
            'track_type': track_type,
            'direction': direction,
            'condition': condition,
            'heads_count': heads_count,
            'entries': [{'umaban': e[0], 'horse_name': e[1], 'jockey': e[2],
                         'odds': e[3], 'popularity': e[4], 'stable': e[6],
                         'body_weight_diff': e[7]} for e in entries],
        }

        scored = []
        for e in entries:
            umaban, horse_name, jockey, odds, popularity, finish, stable, body_weight_diff = e
            sc, bkd = score_horse(conn, horse_name, race_info, before_date=d)
            scored.append({
                'umaban': umaban,
                'horse_name': horse_name,
                'jockey': jockey,
                'odds': odds,
                'popularity': popularity,
                'finish': finish,
                'score': sc,
                'breakdown': bkd,
            })

        scored.sort(key=lambda x: x['score'], reverse=True)
        pick_kwargs = {'race_class_code': class_code or '', 'venue_name': venue_name}
        if _pick_min_score is not None: pick_kwargs['min_score'] = _pick_min_score
        if _pick_min_odds is not None: pick_kwargs['min_odds'] = _pick_min_odds
        if _pick_max_odds is not None: pick_kwargs['max_odds'] = _pick_max_odds
        if _pick_gap_req is not None: pick_kwargs['gap_req'] = _pick_gap_req
        if _pick_max_gap_req is not None: pick_kwargs['max_gap_req'] = _pick_max_gap_req
        honmei, reason = pick_honmei(scored, **pick_kwargs)

        if honmei is None:
            continue

        honmei_umaban = honmei['umaban']
        actual = next((e for e in entries if e[0] == honmei_umaban), None)
        if not actual:
            continue

        finish = actual[5]
        odds = honmei.get('odds') or 0

        cost = 1000
        ret = int(odds * 100) if finish == 1 else 0
        ret = ret * 10  # 100円→1000円換算
        ret_w = winsorize(ret)

        total_cost += cost
        total_ret += ret
        total_ret_w += ret_w
        if finish == 1:
            wins += 1

        records.append({
            'date': d,
            'race_id': race_id,
            'venue': venue_name,
            'race_num': race_num,
            'class_code': class_code or '',
            'distance': distance,
            'condition': condition or '',
            'honmei': honmei['horse_name'],
            'finish': finish,
            'odds': odds,
            'score': honmei['score'],
            'cost': cost,
            'ret': ret,
            'ret_w': ret_w,
        })

    n = len(records)
    roi = total_ret / total_cost * 100 if total_cost else 0
    roi_w = total_ret_w / total_cost * 100 if total_cost else 0
    win_rate = wins / n * 100 if n else 0

    print(f"期間: {since} 〜 {until}")
    print(f"買いレース: {n}R")
    print(f"勝数: {wins} ({win_rate:.1f}%)")
    print(f"投資: {total_cost:,}円 / 回収: {total_ret:,}円 / ROI: {roi:.1f}%")
    print(f"Winsorized ROI ({WINSORIZE_CAP//10000}万円キャップ): {roi_w:.1f}%")

    return {
        'since': since,
        'until': until,
        'n': n,
        'wins': wins,
        'win_rate': round(win_rate, 2),
        'cost': total_cost,
        'ret': total_ret,
        'ret_w': total_ret_w,
        'roi': round(roi, 2),
        'roi_w': round(roi_w, 2),
        'records': records,
    }


def print_breakdown(records, key='venue'):
    """会場別 or クラス別集計"""
    buckets = defaultdict(lambda: {'n': 0, 'wins': 0, 'cost': 0, 'ret': 0})
    for r in records:
        k = r.get(key, '?')
        buckets[k]['n'] += 1
        buckets[k]['wins'] += 1 if r['finish'] == 1 else 0
        buckets[k]['cost'] += r['cost']
        buckets[k]['ret'] += r['ret']

    print(f"\n--- {key}別 ---")
    hdr = f"{'':8s} {'N':>4s} {'勝':>4s} {'勝率':>6s} {'投資':>8s} {'回収':>8s} {'ROI':>6s}"
    print(hdr)
    for k in sorted(buckets.keys()):
        b = buckets[k]
        roi = b['ret'] / b['cost'] * 100 if b['cost'] else 0
        wr = b['wins'] / b['n'] * 100 if b['n'] else 0
        print(f"{k:8s} {b['n']:4d} {b['wins']:4d} {wr:5.1f}%  "
              f"{b['cost']:>8,}  {b['ret']:>8,}  {roi:5.1f}%")


def print_monthly_breakdown(records):
    """月別集計"""
    buckets = defaultdict(lambda: {'n': 0, 'wins': 0, 'cost': 0, 'ret': 0})
    for r in records:
        ym = r['date'][:7]
        buckets[ym]['n'] += 1
        buckets[ym]['wins'] += 1 if r['finish'] == 1 else 0
        buckets[ym]['cost'] += r['cost']
        buckets[ym]['ret'] += r['ret']

    print(f"\n--- 月別 ---")
    for ym in sorted(buckets.keys()):
        b = buckets[ym]
        roi = b['ret'] / b['cost'] * 100 if b['cost'] else 0
        sign = '+' if b['ret'] >= b['cost'] else '-'
        print(f"{ym}  {b['n']:3d}R  勝{b['wins']}  ROI{roi:5.1f}%  {sign}{abs(b['ret']-b['cost']):,}円")


def run_walkforward(conn, venue=None):
    """年次Walk-Forward: 各年をout-of-sample検証"""
    if venue:
        all_years = conn.execute(
            "SELECT DISTINCT substr(date,1,4) FROM nar_races WHERE venue_name=? ORDER BY 1",
            (venue,)
        ).fetchall()
    else:
        all_years = conn.execute(
            "SELECT DISTINCT substr(date,1,4) FROM nar_races ORDER BY 1"
        ).fetchall()
    years = [r[0] for r in all_years if r[0]]

    if len(years) < 2:
        print("Walk-Forwardには2年以上のデータが必要です")
        return

    venue_label = f" [{venue}]" if venue else ""
    print(f"\n=== Walk-Forward CV (年次){venue_label} ===")
    print(f"対象年: {', '.join(years)}")
    print()

    # 上がり3Fスコアのリーク対策: 年foldごとにその年1/1より前のデータのみで
    # time_indexを再構築して使う(本番nar_time_indexは全期間固定でWF-CVには未来データ混入)
    original_load_time_index = scoring_nar._load_time_index
    all_records = []
    try:
        for year in years:
            since = f'{year}-01-01'
            until = f'{year}-12-31'
            trailing_ti = _build_trailing_time_index(conn, since)
            scoring_nar._TIME_INDEX_CACHE = {}
            scoring_nar._load_time_index = lambda _conn, _ti=trailing_ti: _ti
            result = run_backtest(conn, since, until, venue=venue)
            all_records.extend(result['records'])
            print()
    finally:
        scoring_nar._load_time_index = original_load_time_index
        scoring_nar._TIME_INDEX_CACHE = {}

    # 累計
    total_cost = sum(r['cost'] for r in all_records)
    total_ret = sum(r['ret'] for r in all_records)
    total_ret_w = sum(winsorize(r['ret']) for r in all_records)
    total_wins = sum(1 for r in all_records if r['finish'] == 1)
    n = len(all_records)
    print(f"=== 累計 ===")
    print(f"全期間: {n}R  勝{total_wins}  "
          f"投資{total_cost:,}  回収{total_ret:,}  ROI{total_ret/total_cost*100:.1f}%  "
          f"Win-ROI{total_ret_w/total_cost*100:.1f}%")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--since', default='2022-01-01')
    parser.add_argument('--until', default=str(date.today()))
    parser.add_argument('--year', type=int)
    parser.add_argument('--venue', help='大井/川崎/浦和/船橋')
    parser.add_argument('--breakdown', action='store_true', help='会場/クラス/月別集計')
    parser.add_argument('--walkforward', action='store_true', help='年次Walk-Forward CV')
    parser.add_argument('--save', help='結果JSON保存パス')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--min-score', type=float, default=None, help='スコア閾値 (デフォルト: pick_honmei依存)')
    parser.add_argument('--min-odds', type=float, default=None)
    parser.add_argument('--max-odds', type=float, default=None)
    parser.add_argument('--gap-req', type=float, default=None)
    parser.add_argument('--max-gap-req', type=float, default=None, help='ギャップ上限 (チャレンジ枠: gap_req≤gap<max_gap_req)')
    args = parser.parse_args()

    if args.year:
        args.since = f'{args.year}-01-01'
        args.until = f'{args.year}-12-31'

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    global _pick_min_score, _pick_min_odds, _pick_max_odds, _pick_gap_req, _pick_max_gap_req
    _pick_min_score = args.min_score
    _pick_min_odds = args.min_odds
    _pick_max_odds = args.max_odds
    _pick_gap_req = args.gap_req
    _pick_max_gap_req = args.max_gap_req

    if args.walkforward:
        run_walkforward(conn, venue=args.venue)
        conn.close()
        return

    result = run_backtest(conn, args.since, args.until, args.verbose, venue=args.venue)
    conn.close()

    if args.breakdown and result['records']:
        print_breakdown(result['records'], 'venue')
        print_breakdown(result['records'], 'class_code')
        print_monthly_breakdown(result['records'])

    if args.save:
        save_data = {k: v for k, v in result.items() if k != 'records'}
        save_data['records'] = result['records']
        with open(args.save, 'w', encoding='utf-8') as f:
            json.dump(save_data, f, ensure_ascii=False, indent=2)
        print(f"保存: {args.save}")


if __name__ == '__main__':
    main()
