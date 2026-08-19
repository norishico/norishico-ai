"""verify_time_index_leak_fix.py - 上がり3Fスコア(time_index)のリーク検証(read-only)

nar_time_index は全期間(2022-2026)一括で構築された固定テーブルで、
WF-CVの各年foldをスコアリングする際も日付を問わず同一テーブルを参照している
(scoring_nar._load_time_index に before_date 引数なし、プロセス内グローバルキャッシュ)。
これはWF-CVにとって未来データの混入(リーク)にあたる。

本スクリプトは scoring_nar.py / nar_time_index テーブル / nar_keiba.db の
いずれも変更せず、Python側で _load_time_index をランタイムにモンキーパッチして
「その年より前のデータだけで再構築したtime_index」を使うWF-CVを再現する。

3パターンを比較:
  A) 現行(リークあり) - 本番の nar_time_index をそのまま使用
  B) リーク修正(trailing) - 年foldごとにその年1/1より前のデータのみで再構築
  C) 上がり3Fスコアなし - _calc_last3f_score を常に(0.0, {})にパッチ
"""
import sys, sqlite3, statistics
from pathlib import Path
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

PROJ = Path(__file__).parent.parent
DB_PATH = PROJ / 'nar_keiba.db'
sys.path.insert(0, str(Path(__file__).parent))

import scoring_nar
import backtest_nar

MIN_N = 20


def build_trailing_time_index(conn, before_date, min_n=MIN_N):
    """before_date より前のレースだけを使って (venue,distance,condition)->(avg_t,std_t,avg_l3f,std_l3f) を構築(DBには書かない)"""
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
    return time_index, len(rows)


def _calc_last3f_score_none(recent, time_index):
    return 0.0, {}


def run_variant(conn, label, years, load_time_index_patch=None, calc_l3f_patch=None):
    original_load = scoring_nar._load_time_index
    original_calc = scoring_nar._calc_last3f_score
    all_records = []
    year_rows = []
    try:
        for year in years:
            scoring_nar._TIME_INDEX_CACHE = {}
            if load_time_index_patch is not None:
                scoring_nar._load_time_index = load_time_index_patch(year)
            else:
                scoring_nar._load_time_index = original_load
            if calc_l3f_patch is not None:
                scoring_nar._calc_last3f_score = calc_l3f_patch
            else:
                scoring_nar._calc_last3f_score = original_calc

            since, until = f'{year}-01-01', f'{year}-12-31'
            result = backtest_nar.run_backtest(conn, since, until, verbose=False)
            all_records.extend(result['records'])
            cost, ret = result['cost'], result['ret']
            roi = ret / cost * 100 if cost else 0
            year_rows.append((year, result['n'], result['wins'], cost, ret, roi))
    finally:
        scoring_nar._load_time_index = original_load
        scoring_nar._calc_last3f_score = original_calc
        scoring_nar._TIME_INDEX_CACHE = {}

    total_cost = sum(r['cost'] for r in all_records)
    total_ret = sum(r['ret'] for r in all_records)
    total_ret_w = sum(backtest_nar.winsorize(r['ret']) for r in all_records)
    n = len(all_records)
    roi = total_ret / total_cost * 100 if total_cost else 0
    roi_w = total_ret_w / total_cost * 100 if total_cost else 0

    print(f"\n=== {label} ===")
    print(f"{'年':6s} {'N':>5s} {'勝':>4s} {'投資':>9s} {'回収':>10s} {'ROI':>7s}")
    for year, yn, ywins, ycost, yret, yroi in year_rows:
        print(f"{year:6s} {yn:5d} {ywins:4d} {ycost:9,d} {yret:10,d} {yroi:6.1f}%")
    print(f"---累計--- N={n} 投資{total_cost:,} 回収{total_ret:,} "
          f"ROI={roi:.1f}% WinROI={roi_w:.1f}%")

    return {'label': label, 'n': n, 'cost': total_cost, 'ret': total_ret,
            'roi': roi, 'roi_w': roi_w, 'year_rows': year_rows}


def main():
    conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    conn.execute("PRAGMA query_only=1")
    conn.execute("PRAGMA cache_size=-65536")

    years = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(date,1,4) FROM nar_races ORDER BY 1"
    ).fetchall() if r[0]]
    print(f"対象年: {years}")

    # A) 現行(リークあり、本番nar_time_indexそのまま)
    result_a = run_variant(conn, "A) 現行(リークあり・本番nar_time_index)", years)

    # B) リーク修正(trailing、年foldごとにその年1/1より前のデータのみで再構築)
    trailing_cache = {}
    coverage_report = []

    def trailing_patch_factory(year):
        before_date = f'{year}-01-01'
        if before_date not in trailing_cache:
            ti, n_rows = build_trailing_time_index(conn, before_date)
            trailing_cache[before_date] = ti
            coverage_report.append((year, len(ti), n_rows))
        fixed_ti = trailing_cache[before_date]
        return lambda _conn: fixed_ti

    result_b = run_variant(conn, "B) リーク修正版(trailing, expanding window)", years,
                            load_time_index_patch=trailing_patch_factory)

    print("\n--- trailing time_indexのfold別カバレッジ ---")
    print(f"{'年':6s} {'条件数(venue*dist*cond)':>20s} {'訓練データ行数':>12s}")
    for year, n_ti, n_rows in coverage_report:
        print(f"{year:6s} {n_ti:20d} {n_rows:12d}")

    # C) 上がり3Fスコアなしベースライン
    result_c = run_variant(conn, "C) 上がり3Fスコアなしベースライン", years,
                            calc_l3f_patch=_calc_last3f_score_none)

    conn.close()

    print("\n\n=== 3パターン比較サマリ ===")
    print(f"{'':40s} {'N':>5s} {'ROI':>8s} {'WinROI':>8s}")
    for r in (result_a, result_b, result_c):
        print(f"{r['label']:40s} {r['n']:5d} {r['roi']:7.1f}% {r['roi_w']:7.1f}%")


if __name__ == '__main__':
    main()
