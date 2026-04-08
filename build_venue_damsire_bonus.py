"""会場×距離×母父ボーナステーブルの構築
venue_sire_bonusと同じ手法でdam_sire版を構築
"""
import sqlite3
from collections import defaultdict

def build_venue_damsire_bonus(conn, cutoff_date='2099-01-01', start_date=None, min_n=30, min_diff=12, min_stability=0.5):
    """会場×距離×母父のボーナステーブルを構築"""
    date_lower = f"AND date >= '{start_date}'" if start_date else ""
    rows = conn.execute(f"""
        SELECT venue, distance, surface, TRIM(dam_sire) as dam_sire, finish, date
        FROM results
        WHERE finish IS NOT NULL AND finish < 90 AND finish > 0
          AND dam_sire IS NOT NULL AND TRIM(dam_sire) != ''
          AND date < ?
          {date_lower}
    """, (cutoff_date,)).fetchall()

    if not rows: return 0

    # dam_sire × surface × 距離帯の全体複勝率
    ds_overall = defaultdict(lambda: [0, 0])
    for r in rows:
        dist_bucket = 'S' if r['distance'] < 1400 else 'M' if r['distance'] < 2000 else 'L'
        key = (r['dam_sire'], r['surface'], dist_bucket)
        ds_overall[key][0] += 1
        if r['finish'] <= 3: ds_overall[key][1] += 1

    # venue × distance × dam_sire の会場単位複勝率（年度別）
    vds = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in rows:
        key = (r['venue'], r['distance'], r['dam_sire'])
        yr = r['date'][:4]
        vds[key][yr][0] += 1
        if r['finish'] <= 3: vds[key][yr][1] += 1

    conn.execute("DROP TABLE IF EXISTS venue_damsire_bonus")
    conn.execute("""
        CREATE TABLE venue_damsire_bonus (
            venue TEXT, distance INTEGER, dam_sire TEXT,
            n INTEGER, venue_t3r REAL, overall_t3r REAL, diff REAL,
            bonus REAL, stability REAL,
            PRIMARY KEY (venue, distance, dam_sire)
        )
    """)

    count = 0
    for (venue, dist, dam_sire), yearly in vds.items():
        total_n = sum(v[0] for v in yearly.values())
        total_t3 = sum(v[1] for v in yearly.values())
        if total_n < min_n: continue

        venue_t3r = total_t3 / total_n * 100

        # 全体複勝率
        surface_rows = [r for r in rows if r['dam_sire'] == dam_sire and r['venue'] == venue]
        if not surface_rows: continue
        surface = surface_rows[0]['surface']
        dist_bucket = 'S' if dist < 1400 else 'M' if dist < 2000 else 'L'
        overall_key = (dam_sire, surface, dist_bucket)
        if overall_key not in ds_overall: continue
        on, ot3 = ds_overall[overall_key]
        if on < 20: continue
        overall_t3r = ot3 / on * 100

        diff = venue_t3r - overall_t3r
        if diff < min_diff: continue

        # 安定性
        years_data = [(yr, v) for yr, v in yearly.items() if v[0] >= 3]
        if len(years_data) < 2: continue
        years_ok = sum(1 for yr, v in years_data if v[1] / v[0] * 100 >= 25)
        stability = years_ok / len(years_data)
        if stability < min_stability: continue

        bonus = round(min(max(diff * 0.10, 1.0), 3.0), 1)

        conn.execute(
            "INSERT OR REPLACE INTO venue_damsire_bonus VALUES (?,?,?,?,?,?,?,?,?)",
            (venue, dist, dam_sire, total_n, round(venue_t3r, 1), round(overall_t3r, 1),
             round(diff, 1), bonus, round(stability, 2))
        )
        count += 1

    conn.commit()
    print(f"  venue_damsire_bonus: {count}件構築 (cutoff={cutoff_date})")
    return count


if __name__ == '__main__':
    conn = sqlite3.connect('keiba.db')
    conn.row_factory = sqlite3.Row
    build_venue_damsire_bonus(conn)

    print("\n=== venue_damsire_bonus TOP20 ===")
    for r in conn.execute("SELECT * FROM venue_damsire_bonus ORDER BY diff DESC LIMIT 20").fetchall():
        print(f"  {r['venue']:>4} {r['distance']:>5}m {r['dam_sire']:>20} n={r['n']:>4} "
              f"venue={r['venue_t3r']:>5.1f}% overall={r['overall_t3r']:>5.1f}% "
              f"diff={r['diff']:>+5.1f} bonus={r['bonus']:>3.1f} stab={r['stability']:.0%}")

    total = conn.execute("SELECT COUNT(*) FROM venue_damsire_bonus").fetchone()[0]
    print(f"\n合計: {total}件")
