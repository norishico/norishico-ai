"""会場×距離×父ボーナステーブルの構築
リーク防止: cutoff_date以前のデータのみで統計を計算
乖離+15pt以上 & 30件以上 & 安定度60%以上のパターンにボーナス付与
"""
import sqlite3
from collections import defaultdict

def build_venue_sire_bonus(conn, cutoff_date='2099-01-01', start_date=None, min_n=30, min_diff=12, min_stability=0.5):
    """会場×距離×父のボーナステーブルを構築

    1. cutoff_date前の全データからsire×surface×距離帯の全体複勝率を計算
    2. cutoff_date前の全データからvenue×distance×sireの会場単位複勝率を計算
    3. 乖離が大きく安定しているパターンにボーナスを付与
    start_date: 指定時はこの日付以降のデータのみ使用（血統データ期間の制御）
    """
    # 全体複勝率（sire×surface×距離帯）
    date_lower = f"AND date >= '{start_date}'" if start_date else ""
    rows = conn.execute(f"""
        SELECT venue, distance, surface, TRIM(sire) as sire, finish, date
        FROM results
        WHERE finish IS NOT NULL AND finish < 90 AND finish > 0
          AND sire IS NOT NULL AND TRIM(sire) != ''
          AND date < ?
          {date_lower}
    """, (cutoff_date,)).fetchall()

    if not rows:
        return

    # sire × surface × 距離帯の全体複勝率
    sire_overall = defaultdict(lambda: [0, 0])
    for r in rows:
        dist_bucket = 'S' if r['distance'] < 1400 else 'M' if r['distance'] < 2000 else 'L'
        key = (r['sire'], r['surface'], dist_bucket)
        sire_overall[key][0] += 1
        if r['finish'] <= 3: sire_overall[key][1] += 1

    # venue × distance × sire の会場単位複勝率（年度別も）
    vds = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # {(v,d,s): {year: [n, t3]}}
    for r in rows:
        key = (r['venue'], r['distance'], r['sire'])
        yr = r['date'][:4]
        vds[key][yr][0] += 1
        if r['finish'] <= 3: vds[key][yr][1] += 1

    # ボーナス計算
    conn.execute("DROP TABLE IF EXISTS venue_sire_bonus")
    conn.execute("""
        CREATE TABLE venue_sire_bonus (
            venue TEXT, distance INTEGER, sire TEXT,
            n INTEGER, venue_t3r REAL, overall_t3r REAL, diff REAL,
            bonus REAL, stability REAL,
            PRIMARY KEY (venue, distance, sire)
        )
    """)

    count = 0
    for (venue, dist, sire), yearly in vds.items():
        total_n = sum(v[0] for v in yearly.values())
        total_t3 = sum(v[1] for v in yearly.values())
        if total_n < min_n: continue

        venue_t3r = total_t3 / total_n * 100

        # 全体複勝率を取得
        surface_rows = [r for r in rows if r['sire'] == sire and r['venue'] == venue]
        if not surface_rows: continue
        surface = surface_rows[0]['surface']
        dist_bucket = 'S' if dist < 1400 else 'M' if dist < 2000 else 'L'
        overall_key = (sire, surface, dist_bucket)
        if overall_key not in sire_overall: continue
        on, ot3 = sire_overall[overall_key]
        if on < 20: continue
        overall_t3r = ot3 / on * 100

        diff = venue_t3r - overall_t3r
        if diff < min_diff: continue

        # 安定性チェック
        years_data = [(yr, v) for yr, v in yearly.items() if v[0] >= 3]
        if len(years_data) < 2: continue
        years_ok = sum(1 for yr, v in years_data if v[1] / v[0] * 100 >= 25)
        stability = years_ok / len(years_data)
        if stability < min_stability: continue

        # ボーナス: 乖離幅に比例（+15pt乖離→+2pt, +25pt乖離→+4pt）
        bonus = round(min(max(diff * 0.15, 1.5), 5.0), 1)

        conn.execute(
            "INSERT OR REPLACE INTO venue_sire_bonus VALUES (?,?,?,?,?,?,?,?,?)",
            (venue, dist, sire, total_n, round(venue_t3r, 1), round(overall_t3r, 1),
             round(diff, 1), bonus, round(stability, 2))
        )
        count += 1

    conn.commit()
    print(f"  venue_sire_bonus: {count}件構築 (cutoff={cutoff_date})")
    return count


if __name__ == '__main__':
    conn = sqlite3.connect('keiba.db')
    conn.row_factory = sqlite3.Row
    # 全データで構築（確認用）
    build_venue_sire_bonus(conn)

    # 内容確認
    print("\n=== venue_sire_bonus テーブル ===")
    for r in conn.execute("SELECT * FROM venue_sire_bonus ORDER BY diff DESC").fetchall():
        print(f"  {r['venue']:>4} {r['distance']:>5}m {r['sire']:>20} n={r['n']:>4} "
              f"venue={r['venue_t3r']:>5.1f}% overall={r['overall_t3r']:>5.1f}% "
              f"diff={r['diff']:>+5.1f} bonus={r['bonus']:>3.1f} stab={r['stability']:.0%}")
