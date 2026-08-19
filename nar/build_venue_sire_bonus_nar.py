"""build_venue_sire_bonus_nar.py - 南関東版 会場×父系ボーナステーブル構築

⚠️ 非推奨 (2026-08-19): 全期間データを日付フィルタなしで集計する設計のため
WF-CVにリークする(scoring_nar.pyの_get_sire_bonus呼び出しは削除済み)。
リーク修正版はWinsorized WF ROIがボーナスなしベースラインを下回ることを確認済み。
本番では使用しない。

使い方:
  python nar/build_venue_sire_bonus_nar.py
  python nar/build_venue_sire_bonus_nar.py --min-n 20 --min-diff 8
"""
import sys, sqlite3, argparse
from pathlib import Path
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

PROJ = Path(__file__).parent.parent
DB_PATH = PROJ / 'nar_keiba.db'

DEFAULT_MIN_N    = 20    # 最低出走数
DEFAULT_MIN_DIFF = 8.0   # 平均比 +8%pt 以上
MAX_BONUS        = 3.0   # 上限ボーナス点


def build(conn, min_n=DEFAULT_MIN_N, min_diff=DEFAULT_MIN_DIFF, verbose=True):
    # venue × sire の勝率を集計
    rows = conn.execute("""
        SELECT rc.venue_name, p.sire, r.finish
        FROM nar_results r
        JOIN nar_races rc ON r.race_id = rc.race_id
        JOIN nar_horse_pedigree p ON r.horse_name = p.horse_name
        WHERE p.sire IS NOT NULL AND r.finish IS NOT NULL
    """).fetchall()

    if not rows:
        print("ERROR: nar_horse_pedigreeにデータがありません")
        return 0

    # venue × sire 集計
    stats = defaultdict(lambda: {'n': 0, 'wins': 0})
    venue_total = defaultdict(lambda: {'n': 0, 'wins': 0})
    for venue, sire, finish in rows:
        stats[(venue, sire)]['n'] += 1
        venue_total[venue]['n'] += 1
        if finish == 1:
            stats[(venue, sire)]['wins'] += 1
            venue_total[venue]['wins'] += 1

    # 全体平均勝率(会場別)
    venue_avg = {v: d['wins'] / d['n'] if d['n'] else 0
                 for v, d in venue_total.items()}

    # ボーナス計算
    bonus_rows = []
    for (venue, sire), d in stats.items():
        if d['n'] < min_n:
            continue
        wr = d['wins'] / d['n']
        avg = venue_avg.get(venue, 0)
        diff = (wr - avg) * 100  # %pt差
        if diff < min_diff:
            continue
        # 線形スケール: diff=min_diff→0.5pt, diff=25pt→MAX_BONUS
        bonus = min(MAX_BONUS, 0.5 + (diff - min_diff) / (25 - min_diff) * (MAX_BONUS - 0.5))
        bonus = round(bonus, 2)
        bonus_rows.append((venue, sire, d['n'], round(wr * 100, 2),
                           round(avg * 100, 2), round(diff, 2), bonus))

    # テーブル再構築
    conn.execute("DROP TABLE IF EXISTS nar_venue_sire_bonus")
    conn.execute("""
        CREATE TABLE nar_venue_sire_bonus (
            venue_name TEXT,
            sire       TEXT,
            n          INTEGER,
            win_rate   REAL,
            avg_rate   REAL,
            diff       REAL,
            bonus      REAL,
            PRIMARY KEY (venue_name, sire)
        )
    """)
    conn.executemany(
        "INSERT INTO nar_venue_sire_bonus VALUES (?,?,?,?,?,?,?)",
        bonus_rows
    )
    conn.commit()

    if verbose:
        print(f"nar_venue_sire_bonus: {len(bonus_rows)}件 "
              f"(min_n={min_n}, min_diff={min_diff}%pt)")
        for r in sorted(bonus_rows, key=lambda x: -x[6])[:15]:
            print(f"  {r[0]:4s} {r[1]:20s} n={r[2]:4d} "
                  f"勝率{r[3]:5.1f}% avg{r[4]:5.1f}% diff{r[5]:+5.1f}pt → {r[6]:.2f}pt")

    return len(bonus_rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--min-n',    type=int,   default=DEFAULT_MIN_N)
    parser.add_argument('--min-diff', type=float, default=DEFAULT_MIN_DIFF)
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    n = build(conn, args.min_n, args.min_diff)
    conn.close()
    print(f"完了: {n}件")


if __name__ == '__main__':
    main()
