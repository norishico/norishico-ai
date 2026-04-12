"""枠番グループ×父 ボーナステーブルの構築
+ 馬場(重/不良)×父 ボーナステーブルの構築
"""
import sqlite3
from collections import defaultdict

def gate_group(umaban, num_horses):
    if num_horses <= 0: return None
    ratio = umaban / num_horses
    if ratio <= 0.375: return '内(1-3枠)'
    elif ratio <= 0.750: return '中(4-6枠)'
    else: return '外(7-8枠)'


def build_gate_sire_bonus(conn, cutoff_date='2099-01-01', min_n=15, min_stability=0.5):
    """枠番グループ×父のボーナステーブルを構築"""
    rows = conn.execute("""
        SELECT TRIM(sire) as sire, finish, odds, umaban, num_horses, date
        FROM results
        WHERE finish IS NOT NULL AND finish < 90 AND finish > 0
          AND odds IS NOT NULL AND odds > 0
          AND sire IS NOT NULL AND TRIM(sire) != ''
          AND umaban > 0 AND num_horses > 0
          AND date < ?
    """, (cutoff_date,)).fetchall()

    if not rows: return 0

    # sire全体の複勝率
    sire_overall = defaultdict(lambda: [0, 0])
    for r in rows:
        sire_overall[r['sire']][0] += 1
        if r['finish'] <= 3: sire_overall[r['sire']][1] += 1

    # 枠番グループ×sire の年度別
    gs_yearly = defaultdict(lambda: defaultdict(lambda: [0, 0, 0.0]))
    for r in rows:
        gg = gate_group(r['umaban'], r['num_horses'])
        if gg is None: continue
        key = (gg, r['sire'])
        yr = r['date'][:4]
        gs_yearly[key][yr][0] += 1
        if r['finish'] <= 3: gs_yearly[key][yr][1] += 1
        if r['finish'] == 1: gs_yearly[key][yr][2] += r['odds']

    conn.execute("DROP TABLE IF EXISTS gate_sire_bonus")
    conn.execute("""
        CREATE TABLE gate_sire_bonus (
            gate_group TEXT, sire TEXT,
            n INTEGER, gate_t3r REAL, overall_t3r REAL, diff REAL,
            bonus REAL, stability REAL, tan_roi REAL,
            PRIMARY KEY (gate_group, sire)
        )
    """)

    count = 0
    for (gg, sire), yearly in gs_yearly.items():
        total_n = sum(v[0] for v in yearly.values())
        total_t3 = sum(v[1] for v in yearly.values())
        total_odds = sum(v[2] for v in yearly.values())
        if total_n < min_n: continue
        gate_t3r = total_t3 / total_n * 100
        tan_roi = total_odds / total_n * 100

        # 全体複勝率
        on, ot3 = sire_overall.get(sire, (0, 0))
        if on < 20: continue
        overall_t3r = ot3 / on * 100
        diff = gate_t3r - overall_t3r
        if not (gate_t3r >= 80 or tan_roi >= 150): continue
        if diff < 8: continue  # 乖離8pt以上

        # 安定性
        years_data = [(yr, v) for yr, v in yearly.items() if v[0] >= 2]
        if len(years_data) < 2: continue
        years_ok = sum(1 for yr, v in years_data if v[1] / v[0] * 100 >= 20)
        stability = years_ok / len(years_data)
        if stability < min_stability: continue

        bonus = round(min(max(diff * 0.12, 1.0), 4.0), 1)

        conn.execute(
            "INSERT OR REPLACE INTO gate_sire_bonus VALUES (?,?,?,?,?,?,?,?,?)",
            (gg, sire, total_n, round(gate_t3r, 1), round(overall_t3r, 1),
             round(diff, 1), bonus, round(stability, 2), round(tan_roi, 1))
        )
        count += 1

    conn.commit()
    print(f"  gate_sire_bonus: {count}件構築 (cutoff={cutoff_date})")
    return count


def build_track_cond_sire_bonus(conn, cutoff_date='2099-01-01', min_n=10, min_stability=0.4):
    """馬場(重/不良)×父のボーナステーブルを構築"""
    rows = conn.execute("""
        SELECT TRIM(sire) as sire, finish, odds, track_cond, date
        FROM results
        WHERE finish IS NOT NULL AND finish < 90 AND finish > 0
          AND odds IS NOT NULL AND odds > 0
          AND sire IS NOT NULL AND TRIM(sire) != ''
          AND track_cond IN ('重', '不')
          AND date < ?
    """, (cutoff_date,)).fetchall()

    if not rows: return 0

    # sire全体の複勝率（重/不良限定）
    sire_all_heavy = defaultdict(lambda: [0, 0])
    for r in rows:
        sire_all_heavy[r['sire']][0] += 1
        if r['finish'] <= 3: sire_all_heavy[r['sire']][1] += 1

    # 全体平均（重/不良の全馬複勝率）
    total_all = sum(v[0] for v in sire_all_heavy.values())
    total_t3 = sum(v[1] for v in sire_all_heavy.values())
    global_t3r = total_t3 / total_all * 100 if total_all else 22.0

    # 馬場×sire の年度別
    ts_yearly = defaultdict(lambda: defaultdict(lambda: [0, 0, 0.0]))
    for r in rows:
        cond_label = '重' if r['track_cond'] == '重' else '不良'
        key = (cond_label, r['sire'])
        yr = r['date'][:4]
        ts_yearly[key][yr][0] += 1
        if r['finish'] <= 3: ts_yearly[key][yr][1] += 1
        if r['finish'] == 1: ts_yearly[key][yr][2] += r['odds']

    conn.execute("DROP TABLE IF EXISTS track_cond_sire_bonus")
    conn.execute("""
        CREATE TABLE track_cond_sire_bonus (
            track_cond TEXT, sire TEXT,
            n INTEGER, cond_t3r REAL, overall_t3r REAL, diff REAL,
            bonus REAL, stability REAL, tan_roi REAL,
            PRIMARY KEY (track_cond, sire)
        )
    """)

    count = 0
    for (cond, sire), yearly in ts_yearly.items():
        total_n = sum(v[0] for v in yearly.values())
        total_t3 = sum(v[1] for v in yearly.values())
        total_odds = sum(v[2] for v in yearly.values())
        if total_n < min_n: continue
        cond_t3r = total_t3 / total_n * 100
        tan_roi = total_odds / total_n * 100

        if not (cond_t3r >= 80 or tan_roi >= 150): continue

        # 全体との乖離
        diff = cond_t3r - global_t3r
        if diff < 5: continue  # 重/不良全体平均との乖離5pt以上

        # 安定性
        years_data = [(yr, v) for yr, v in yearly.items() if v[0] >= 2]
        if len(years_data) < 2: continue
        years_ok = sum(1 for yr, v in years_data if v[1] / v[0] * 100 >= 20)
        stability = years_ok / len(years_data)
        if stability < min_stability: continue

        bonus = round(min(max(diff * 0.15, 1.0), 4.0), 1)

        conn.execute(
            "INSERT OR REPLACE INTO track_cond_sire_bonus VALUES (?,?,?,?,?,?,?,?,?)",
            (cond, sire, total_n, round(cond_t3r, 1), round(global_t3r, 1),
             round(diff, 1), bonus, round(stability, 2), round(tan_roi, 1))
        )
        count += 1

    conn.commit()
    print(f"  track_cond_sire_bonus: {count}件構築 (cutoff={cutoff_date})")
    return count


if __name__ == '__main__':
    conn = sqlite3.connect('keiba.db')
    conn.row_factory = sqlite3.Row
    build_gate_sire_bonus(conn)
    build_track_cond_sire_bonus(conn)

    print("\n=== gate_sire_bonus TOP15 ===")
    for r in conn.execute("SELECT * FROM gate_sire_bonus ORDER BY diff DESC LIMIT 15").fetchall():
        print(f"  {r['gate_group']:>12} {r['sire']:>20} n={r['n']:>4} t3r={r['gate_t3r']:>5.1f}% diff={r['diff']:>+5.1f} bonus={r['bonus']:>3.1f} stab={r['stability']:.0%}")

    print("\n=== track_cond_sire_bonus TOP15 ===")
    for r in conn.execute("SELECT * FROM track_cond_sire_bonus ORDER BY diff DESC LIMIT 15").fetchall():
        print(f"  {r['track_cond']:>4} {r['sire']:>20} n={r['n']:>4} t3r={r['cond_t3r']:>5.1f}% diff={r['diff']:>+5.1f} bonus={r['bonus']:>3.1f} roi={r['tan_roi']:>5.1f}%")
