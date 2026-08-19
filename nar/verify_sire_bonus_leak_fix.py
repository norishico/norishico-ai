"""verify_sire_bonus_leak_fix.py - 血統ボーナスのリーク検証(read-only)

nar_venue_sire_bonus は全期間(2022-2026)一括で構築された固定テーブルで、
WF-CVの各年foldをスコアリングする際も日付を問わず同一テーブルを参照している
(scoring_nar._get_sire_bonus に before_date 引数なし)。
これはWF-CVにとって未来データの混入(リーク)にあたる。

本スクリプトは scoring_nar.py / nar_venue_sire_bonus テーブル / nar_keiba.db
のいずれも変更せず、Python側で _get_sire_bonus をランタイムにモンキーパッチして
「その年より前のデータだけで再構築したボーナス」を使うWF-CVを再現する。

3パターンを比較:
  A) 現行(リークあり) - 本番の nar_venue_sire_bonus をそのまま使用
  B) リーク修正(trailing) - 年foldごとにその年の1/1より前のデータのみで再構築
  C) ボーナスなし - _get_sire_bonus を常に0を返すようにパッチ
"""
import sys, sqlite3, argparse
from pathlib import Path
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

PROJ = Path(__file__).parent.parent
DB_PATH = PROJ / 'nar_keiba.db'
sys.path.insert(0, str(Path(__file__).parent))

import scoring_nar
import backtest_nar

MIN_N = 20
MIN_DIFF = 8.0
MAX_BONUS = 3.0


def build_trailing_bonus_dict(conn, before_date, min_n=MIN_N, min_diff=MIN_DIFF):
    """before_date より前のレースだけを使って venue×sire ボーナス辞書を構築(DBには書かない)"""
    rows = conn.execute("""
        SELECT rc.venue_name, p.sire, r.finish
        FROM nar_results r
        JOIN nar_races rc ON r.race_id = rc.race_id
        JOIN nar_horse_pedigree p ON r.horse_name = p.horse_name
        WHERE p.sire IS NOT NULL AND r.finish IS NOT NULL
          AND rc.date < ?
    """, (before_date,)).fetchall()

    stats = defaultdict(lambda: {'n': 0, 'wins': 0})
    venue_total = defaultdict(lambda: {'n': 0, 'wins': 0})
    for venue, sire, finish in rows:
        stats[(venue, sire)]['n'] += 1
        venue_total[venue]['n'] += 1
        if finish == 1:
            stats[(venue, sire)]['wins'] += 1
            venue_total[venue]['wins'] += 1

    venue_avg = {v: d['wins'] / d['n'] if d['n'] else 0 for v, d in venue_total.items()}

    bonus_dict = {}
    for (venue, sire), d in stats.items():
        if d['n'] < min_n:
            continue
        wr = d['wins'] / d['n']
        avg = venue_avg.get(venue, 0)
        diff = (wr - avg) * 100
        if diff < min_diff:
            continue
        bonus = min(MAX_BONUS, 0.5 + (diff - min_diff) / (25 - min_diff) * (MAX_BONUS - 0.5))
        bonus_dict[(venue, sire)] = round(bonus, 2)

    return bonus_dict, len(rows)


def make_trailing_fn(conn, bonus_dict):
    def _get_sire_bonus_trailing(_conn, horse_name, venue_name):
        row = conn.execute(
            "SELECT sire FROM nar_horse_pedigree WHERE horse_name = ? LIMIT 1",
            (horse_name.strip(),)
        ).fetchone()
        if not row or not row[0]:
            return 0.0
        return bonus_dict.get((venue_name, row[0]), 0.0)
    return _get_sire_bonus_trailing


def _get_sire_bonus_none(conn, horse_name, venue_name):
    return 0.0


def run_variant(conn, label, years, patch_fn_per_year=None, flat_patch=None):
    original = scoring_nar._get_sire_bonus
    all_records = []
    year_rows = []
    try:
        for year in years:
            if patch_fn_per_year is not None:
                scoring_nar._get_sire_bonus = patch_fn_per_year(year)
            elif flat_patch is not None:
                scoring_nar._get_sire_bonus = flat_patch
            else:
                scoring_nar._get_sire_bonus = original

            since, until = f'{year}-01-01', f'{year}-12-31'
            result = backtest_nar.run_backtest(conn, since, until, verbose=False)
            all_records.extend(result['records'])
            cost, ret = result['cost'], result['ret']
            roi = ret / cost * 100 if cost else 0
            year_rows.append((year, result['n'], result['wins'], cost, ret, roi))
    finally:
        scoring_nar._get_sire_bonus = original

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

    # A) 現行(リークあり、本番テーブルそのまま)
    result_a = run_variant(conn, "A) 現行(リークあり・本番nar_venue_sire_bonus)", years)

    # B) リーク修正(trailing、年foldごとにその年1/1より前のデータのみで再構築)
    trailing_cache = {}
    coverage_report = []

    def trailing_patch_factory(year):
        before_date = f'{year}-01-01'
        if before_date not in trailing_cache:
            bonus_dict, n_rows = build_trailing_bonus_dict(conn, before_date)
            trailing_cache[before_date] = bonus_dict
            coverage_report.append((year, len(bonus_dict), n_rows))
        return make_trailing_fn(conn, trailing_cache[before_date])

    result_b = run_variant(conn, "B) リーク修正版(trailing, expanding window)", years,
                            patch_fn_per_year=trailing_patch_factory)

    print("\n--- trailingボーナステーブルのfold別カバレッジ ---")
    print(f"{'年':6s} {'ボーナス対象sire×venue件数':>20s} {'訓練データ行数':>12s}")
    for year, n_bonus, n_rows in coverage_report:
        print(f"{year:6s} {n_bonus:20d} {n_rows:12d}")

    # C) ボーナスなしベースライン
    result_c = run_variant(conn, "C) ボーナスなしベースライン", years, flat_patch=_get_sire_bonus_none)

    conn.close()

    print("\n\n=== 3パターン比較サマリ ===")
    print(f"{'':40s} {'N':>5s} {'ROI':>8s} {'WinROI':>8s}")
    for r in (result_a, result_b, result_c):
        print(f"{r['label']:40s} {r['n']:5d} {r['roi']:7.1f}% {r['roi_w']:7.1f}%")


if __name__ == '__main__':
    main()
