"""父×母父ニックスボーナステーブルの構築

特定の sire × dam_sire 組み合わせが、sire単独の平均より複勝率が高いパターンを検出。
venue_sire_bonus / cushion_sire_bonus と同手法。
リーク防止: cutoff_date 以前のデータのみ使用。
"""
import sqlite3
from collections import defaultdict


def build_nicks_bonus(conn, cutoff_date='2099-01-01', start_date=None,
                      min_n=100, min_diff=10, min_stability=0.5):
    """nicks_bonusテーブルを構築 (芝限定・信頼度加重・厳格版)
    bonus = min(diff * 0.08, 1.5) × min(n/100, 1.0)
    """

    date_lower = f"AND date >= '{start_date}'" if start_date else ""
    rows = conn.execute(f"""
        SELECT TRIM(sire) as sire, TRIM(dam_sire) as dam_sire, finish, date, surface
        FROM results
        WHERE finish IS NOT NULL AND finish > 0 AND finish < 90
          AND sire IS NOT NULL AND TRIM(sire) != ''
          AND dam_sire IS NOT NULL AND TRIM(dam_sire) != ''
          AND surface = '芝'
          AND date < ?
          {date_lower}
    """, (cutoff_date,)).fetchall()

    if not rows:
        print('  nicks_bonus: no data')
        return 0

    # sire 単独の複勝率 (ベースライン)
    sire_overall = defaultdict(lambda: [0, 0])
    # sire × dam_sire の複勝率 (年度別)
    pair_counts = defaultdict(lambda: [0, 0])
    pair_yearly = defaultdict(lambda: defaultdict(lambda: [0, 0]))

    for r in rows:
        sire = r['sire']
        ds = r['dam_sire']
        hit = 1 if r['finish'] <= 3 else 0
        yr = r['date'][:4]

        sire_overall[sire][0] += 1
        sire_overall[sire][1] += hit

        key = (sire, ds)
        pair_counts[key][0] += 1
        pair_counts[key][1] += hit
        pair_yearly[key][yr][0] += 1
        pair_yearly[key][yr][1] += hit

    conn.execute("DROP TABLE IF EXISTS nicks_bonus")
    conn.execute("""
        CREATE TABLE nicks_bonus (
            sire TEXT, dam_sire TEXT,
            n INTEGER, pair_t3r REAL, sire_t3r REAL, diff REAL,
            bonus REAL, stability REAL,
            PRIMARY KEY (sire, dam_sire)
        )
    """)

    count = 0
    for (sire, ds), (n, t3) in pair_counts.items():
        if n < min_n:
            continue

        pair_t3r = t3 / n * 100

        ovn, ovt3 = sire_overall[sire]
        if ovn < 50:
            continue
        sire_t3r = ovt3 / ovn * 100

        diff = pair_t3r - sire_t3r
        if diff < min_diff:
            continue

        years_data = [(yr, v) for yr, v in pair_yearly[(sire, ds)].items() if v[0] >= 3]
        if len(years_data) < 2:
            continue
        years_ok = sum(1 for _, v in years_data if v[1] / v[0] * 100 >= 25)
        stability = years_ok / len(years_data)
        if stability < min_stability:
            continue

        # 信頼度加重: n が大きいほどボーナスが大きい (n=100で100%, n=200+で上限)
        confidence = min(n / 100.0, 1.0)
        raw_bonus = min(diff * 0.08, 1.5)
        bonus = round(raw_bonus * confidence, 1)
        if bonus < 0.3:
            continue  # 微小ボーナスは除外

        conn.execute(
            "INSERT OR REPLACE INTO nicks_bonus VALUES (?,?,?,?,?,?,?,?)",
            (sire, ds, n, round(pair_t3r, 1), round(sire_t3r, 1),
             round(diff, 1), bonus, round(stability, 2))
        )
        count += 1

    conn.commit()
    print(f"  nicks_bonus: {count}件構築 (cutoff={cutoff_date})")
    return count


if __name__ == '__main__':
    conn = sqlite3.connect('keiba.db')
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.row_factory = sqlite3.Row
    build_nicks_bonus(conn)

    print("\n=== nicks_bonus (diff降順 top20) ===")
    for r in conn.execute("SELECT * FROM nicks_bonus ORDER BY diff DESC LIMIT 20"):
        print(f"  {r['sire']:>18} x {r['dam_sire']:>18} n={r['n']:>4} "
              f"pair={r['pair_t3r']:>5.1f}% sire={r['sire_t3r']:>5.1f}% "
              f"diff={r['diff']:>+5.1f} bonus={r['bonus']:>3.1f} stab={r['stability']:.0%}")
    total = conn.execute("SELECT COUNT(*) FROM nicks_bonus").fetchone()[0]
    print(f"\n合計 {total} 件")
    conn.close()
