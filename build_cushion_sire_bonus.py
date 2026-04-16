"""クッション値×父ボーナステーブルの構築

JRA公式クッション値(芝のみ)と父の相性を集計してscoring補正用テーブル作成。
venue_sire_bonus と同手法: cutoff_date前データで集計+年度安定性チェック。

クッション値ビン (3分割):
  柔 (soft):      <= 8.9
  標準 (normal):  9.0 〜 9.9
  硬 (firm):      >= 10.0

リーク防止: cutoff_date以前のデータのみ使用
乖離+12pt以上 & 30件以上 & 安定度50%以上のパターンにボーナス
ボーナス範囲: +1.0 〜 +3.0 (venue_sire_bonusより保守的、二重計上回避)
"""
import sqlite3
from collections import defaultdict


def cushion_bin(c):
    if c is None:
        return None
    if c <= 8.9:
        return 'soft'
    if c <= 9.9:
        return 'normal'
    return 'firm'


def build_cushion_sire_bonus(conn, cutoff_date='2099-01-01', start_date=None,
                              min_n=30, min_diff=8, min_stability=0.5):
    """クッション値×父のボーナステーブルを構築 (芝のみ)"""

    date_lower = f"AND r.date >= '{start_date}'" if start_date else ""
    rows = conn.execute(f"""
        SELECT r.date, r.venue, r.surface, TRIM(r.sire) as sire, r.finish, cv.cushion
        FROM results r
        JOIN cushion_value cv ON cv.date = r.date AND cv.venue = r.venue
        WHERE r.finish IS NOT NULL AND r.finish < 90 AND r.finish > 0
          AND r.sire IS NOT NULL AND TRIM(r.sire) != ''
          AND r.surface = '芝'
          AND cv.cushion IS NOT NULL
          AND r.date < ?
          {date_lower}
    """, (cutoff_date,)).fetchall()

    if not rows:
        print('  cushion_sire_bonus: データなし')
        return 0

    # sire × cushion_bin 別の集計
    bin_counts = defaultdict(lambda: [0, 0])       # {(sire, bin): [n, t3]}
    sire_overall = defaultdict(lambda: [0, 0])     # {sire: [n, t3]} 芝全体
    yearly = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # {(sire,bin): {year: [n,t3]}}

    for r in rows:
        sire = r['sire']
        cb = cushion_bin(r['cushion'])
        if cb is None:
            continue
        hit = 1 if r['finish'] <= 3 else 0
        bin_counts[(sire, cb)][0] += 1
        bin_counts[(sire, cb)][1] += hit
        sire_overall[sire][0] += 1
        sire_overall[sire][1] += hit
        yr = r['date'][:4]
        yearly[(sire, cb)][yr][0] += 1
        yearly[(sire, cb)][yr][1] += hit

    # テーブル再作成
    conn.execute("DROP TABLE IF EXISTS cushion_sire_bonus")
    conn.execute("""
        CREATE TABLE cushion_sire_bonus (
            cushion_bin TEXT, sire TEXT,
            n INTEGER, bin_t3r REAL, overall_t3r REAL, diff REAL,
            bonus REAL, stability REAL,
            PRIMARY KEY (cushion_bin, sire)
        )
    """)

    count = 0
    for (sire, cb), (n, t3) in bin_counts.items():
        if n < min_n:
            continue

        bin_t3r = t3 / n * 100

        # 全体(芝)の父別複勝率
        ovn, ovt3 = sire_overall[sire]
        if ovn < 50:
            continue
        overall_t3r = ovt3 / ovn * 100

        diff = bin_t3r - overall_t3r
        if diff < min_diff:
            continue

        # 年度安定性: ≥3件ある年で複勝率25%以上を達成している割合
        years_data = [(yr, v) for yr, v in yearly[(sire, cb)].items() if v[0] >= 3]
        if len(years_data) < 2:
            continue
        years_ok = sum(1 for _, v in years_data if v[1] / v[0] * 100 >= 25)
        stability = years_ok / len(years_data)
        if stability < min_stability:
            continue

        # ボーナス: 乖離幅に比例 (+12pt→1.2, +20pt→2.0, +30pt→3.0), 上限3.0
        bonus = round(min(max(diff * 0.10, 1.0), 3.0), 1)

        conn.execute(
            "INSERT OR REPLACE INTO cushion_sire_bonus VALUES (?,?,?,?,?,?,?,?)",
            (cb, sire, n, round(bin_t3r, 1), round(overall_t3r, 1),
             round(diff, 1), bonus, round(stability, 2))
        )
        count += 1

    conn.commit()
    print(f"  cushion_sire_bonus: {count}件構築 (cutoff={cutoff_date})")
    return count


if __name__ == '__main__':
    conn = sqlite3.connect('keiba.db')
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.row_factory = sqlite3.Row

    # 全データで構築
    build_cushion_sire_bonus(conn)

    print("\n=== cushion_sire_bonus テーブル (diff降順) ===")
    print(f"{'bin':<7} {'sire':<22} {'n':>4} {'bin_t3r':>8} {'overall':>8} {'diff':>6} {'bonus':>5} {'stab':>5}")
    for r in conn.execute("SELECT * FROM cushion_sire_bonus ORDER BY diff DESC").fetchall():
        print(f"  {r['cushion_bin']:<5} {r['sire']:<22} {r['n']:>4} "
              f"{r['bin_t3r']:>7.1f}% {r['overall_t3r']:>7.1f}% "
              f"{r['diff']:>+5.1f} {r['bonus']:>4.1f} {r['stability']:.0%}")

    total = conn.execute("SELECT COUNT(*) FROM cushion_sire_bonus").fetchone()[0]
    print(f"\n合計 {total} 件")
    conn.close()
