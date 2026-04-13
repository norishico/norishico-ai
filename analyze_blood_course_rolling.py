"""
さくら案: 血統×コース3元相互作用の時系列安定性検証

各年Yについて、2020-(Y-1)で抽出した強パターンが年Yで再現するかをチェック。
ローリングで3年連続再現するパターンのみを本採用候補とする。
"""
import sqlite3, sys, io
from collections import defaultdict
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

c = sqlite3.connect('keiba.db')
c.row_factory = sqlite3.Row

YEARS = [2020,2021,2022,2023,2024,2025,2026]
# 閾値
MIN_N_TRAIN = 30
MIN_DEV_TRAIN = 12  # 複勝率+12pt 以上乖離


def get_baseline_top3(year_from, year_to):
    """期間内の全体複勝率"""
    r = c.execute(f'''
        SELECT AVG(CASE WHEN finish<=3 THEN 1.0 ELSE 0 END)
        FROM results WHERE date BETWEEN '{year_from}-01-01' AND '{year_to}-12-31' AND finish<90
    ''').fetchone()[0]
    return r * 100 if r else 21.8


def extract_patterns(year_from, year_to, min_n=MIN_N_TRAIN, min_dev=MIN_DEV_TRAIN):
    """期間内のvenue×dist×sire×dam_sireで全体平均+min_dev以上のパターン"""
    baseline = get_baseline_top3(year_from, year_to)
    q = f'''
    SELECT venue, distance, sire, dam_sire, COUNT(*) as n,
           SUM(CASE WHEN finish<=3 THEN 1.0 ELSE 0 END)/COUNT(*) * 100 as top3_rate
    FROM results
    WHERE date BETWEEN '{year_from}-01-01' AND '{year_to}-12-31'
      AND sire != '' AND dam_sire != '' AND finish < 90
    GROUP BY venue, distance, sire, dam_sire
    HAVING n >= {min_n} AND top3_rate >= {baseline + min_dev}
    '''
    rows = c.execute(q).fetchall()
    return {(r['venue'], r['distance'], r['sire'], r['dam_sire']):
            (r['n'], r['top3_rate']) for r in rows}


def test_pattern(pattern, year):
    """年Yでの複勝率を取得"""
    venue, dist, sire, ds = pattern
    r = c.execute('''
        SELECT COUNT(*) as n, SUM(CASE WHEN finish<=3 THEN 1.0 ELSE 0 END) as t3
        FROM results WHERE venue=? AND distance=? AND sire=? AND dam_sire=?
        AND date LIKE ? AND finish<90
    ''', (venue, dist, sire, ds, f'{year}-%')).fetchone()
    if r['n'] == 0:
        return None, 0
    return r['t3']/r['n']*100, r['n']


def main():
    print('='*90)
    print('【血統3元相互作用 時系列安定性検証】')
    print('='*90)
    print(f'訓練閾値: n>={MIN_N_TRAIN}, 全体平均+{MIN_DEV_TRAIN}pt以上')
    print()

    # ローリング検証
    stable_counts = defaultdict(int)  # パターン → テスト年で一定成績出した回数
    pattern_years = defaultdict(list)

    for test_year in [2022, 2023, 2024, 2025, 2026]:
        train_from = 2020
        train_to = test_year - 1
        if train_to < train_from: continue

        patterns = extract_patterns(train_from, train_to)
        baseline = get_baseline_top3(test_year, test_year)

        print(f'  訓練{train_from}-{train_to} で抽出: {len(patterns)}パターン → {test_year}テスト')

        hit = 0
        for p in patterns:
            test_rate, test_n = test_pattern(p, test_year)
            if test_rate is None: continue
            pattern_years[p].append((test_year, test_rate, test_n))
            # ベース+8pt以上維持なら「再現」
            if test_rate >= baseline + 8 and test_n >= 5:
                stable_counts[p] += 1
                hit += 1
        print(f'    → {hit}パターンが再現')

    print()
    print('='*90)
    print('【連続再現パターン (2回以上)】')
    print('='*90)
    stable_sorted = sorted(stable_counts.items(), key=lambda x: -x[1])
    print(f'  2+回再現: {sum(1 for _,v in stable_sorted if v>=2)}パターン')
    print(f'  3+回再現: {sum(1 for _,v in stable_sorted if v>=3)}パターン')
    print(f'  4+回再現: {sum(1 for _,v in stable_sorted if v>=4)}パターン')
    print()

    # 3+回以上再現したパターンを詳細表示
    print('【3+回連続再現した有力パターン】')
    for pattern, cnt in stable_sorted:
        if cnt < 3: continue
        venue, dist, sire, ds = pattern
        hist = pattern_years[pattern]
        hist_str = ', '.join(f'{y}:{r:.0f}%(n={n})' for y, r, n in hist)
        print(f'  [{cnt}/5回] {venue}{dist}m {sire}×{ds}')
        print(f'          {hist_str}')


if __name__ == '__main__':
    main()
