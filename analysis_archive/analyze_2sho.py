"""analyze_2sho.py - 2勝クラス(v6.6廃止)の再検証

廃止根拠: 「的中率0-6%構造不振」「狭帯非現実的」
再検証の観点:
  1. 全年の2勝クラス勝馬オッズ分布
  2. 1-3番人気の勝率(他クラスと比較)
  3. フィールドサイズ別の勝率安定性
  4. 会場別の勝率傾向
  5. 「もし復活させるなら」現実的な帯と期待ROIの机上見積
"""
import io
import sqlite3
import sys
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

DB = r"C:\Users\westr\norishiko_ai\keiba.db"
YEARS = (2020, 2021, 2022, 2023, 2024, 2025)


def grade_2sho(rn):
    s = str(rn or "")
    if "3勝" in s or "３勝" in s or "1600万" in s: return False
    if "2勝" in s or "２勝" in s or "1000万" in s: return True
    return False


def main():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.row_factory = sqlite3.Row

    # 2勝クラスの全レース winner 抽出
    rows = conn.execute("""
        SELECT r.date, r.venue, r.race_num, r.race_name, r.num_horses,
               r.horse_name, r.finish, r.odds, r.popularity, r.surface, r.distance
        FROM results r
        WHERE r.finish = 1
          AND substr(r.date, 1, 4) BETWEEN '2020' AND '2025'
          AND (r.race_name LIKE '%2勝%' OR r.race_name LIKE '%２勝%' OR r.race_name LIKE '%1000万%')
    """).fetchall()
    winners = [dict(r) for r in rows if grade_2sho(r["race_name"])]

    print(f"=== 2勝クラス 全年 勝馬サンプル {len(winners)}R ===\n")

    # 1. 勝馬オッズ分布
    print("--- 勝馬オッズ帯分布 ---")
    bands = [(1,2),(2,3),(3,5),(5,8),(8,11),(11,15),(15,20),(20,30),(30,50),(50,999)]
    total = len(winners)
    for lo, hi in bands:
        n = sum(1 for w in winners if w["odds"] and lo <= w["odds"] < hi)
        pct = n/total*100 if total else 0
        bar = "#" * int(pct)
        print(f"  {lo:>3}-{hi:<3}倍: {n:>4}R ({pct:>5.1f}%) {bar}")

    # 2. 1-3番人気の勝率 (母集団: 全2勝レース)
    all_2sho_races = conn.execute("""
        SELECT DISTINCT date, venue, race_num, race_name
        FROM results
        WHERE substr(date, 1, 4) BETWEEN '2020' AND '2025'
          AND (race_name LIKE '%2勝%' OR race_name LIKE '%２勝%' OR race_name LIKE '%1000万%')
    """).fetchall()
    race_keys = [(r["date"], r["venue"], r["race_num"]) for r in all_2sho_races
                 if grade_2sho(r["race_name"])]
    print(f"\n母集団: 2勝クラス {len(race_keys)}R\n")

    # 人気別勝率
    print("--- 人気別 勝率 (2勝クラス全年) ---")
    pop_win = defaultdict(int)
    pop_total = defaultdict(int)
    for d, v, rn in race_keys:
        runners = conn.execute(
            "SELECT finish, popularity FROM results WHERE date=? AND venue=? AND race_num=? AND finish > 0 AND finish < 90",
            (d, v, rn)
        ).fetchall()
        for row in runners:
            p = row["popularity"]
            if p is None or p < 1: continue
            pop_total[p] += 1
            if row["finish"] == 1:
                pop_win[p] += 1
    for p in range(1, 11):
        n = pop_total[p]
        if n == 0: continue
        w = pop_win[p]
        print(f"  {p:>2}番人気: {w:>4}/{n:<5} 勝率 {w/n*100:>5.1f}%")

    # 参考: 他クラス 1番人気勝率比較
    print("\n--- 参考: クラス別 1番人気勝率 ---")
    classes = [
        ("新馬", "新馬"),
        ("未勝利", "未勝利"),
        ("1勝", "1勝"),
        ("2勝", "2勝"),
        ("3勝", "3勝"),
        ("G3", "G3"),
        ("G2", "G2"),
        ("G1", "G1"),
    ]
    for label, kw in classes:
        rows = conn.execute(f"""
            SELECT COUNT(*) as n,
                   SUM(CASE WHEN finish=1 THEN 1 ELSE 0 END) as w
            FROM results
            WHERE substr(date, 1, 4) BETWEEN '2020' AND '2025'
              AND popularity = 1
              AND finish > 0 AND finish < 90
              AND race_name LIKE '%{kw}%'
        """).fetchone()
        if rows["n"]:
            print(f"  {label:<6}: 1番人気 {rows['w']}/{rows['n']} ({rows['w']/rows['n']*100:.1f}%)")

    # 3. 年次×人気1 勝率
    print("\n--- 2勝クラス 年次×1番人気勝率 ---")
    for y in YEARS:
        r = conn.execute("""
            SELECT COUNT(*) n, SUM(CASE WHEN finish=1 THEN 1 ELSE 0 END) w
            FROM results
            WHERE substr(date,1,4) = ?
              AND popularity = 1 AND finish > 0 AND finish < 90
              AND (race_name LIKE '%2勝%' OR race_name LIKE '%２勝%' OR race_name LIKE '%1000万%')
        """, (str(y),)).fetchone()
        if r["n"]:
            print(f"  {y}: {r['w']}/{r['n']} 1番人気勝率 {r['w']/r['n']*100:.1f}%")

    # 4. 「もし8-11倍に絞ったら」単勝期待値ラフ試算
    print("\n--- 机上試算: 単勝8-11倍の馬を全員買ったら(愚直ベース) ---")
    q = """
        SELECT COUNT(*) n,
               SUM(CASE WHEN finish=1 THEN 1 ELSE 0 END) w,
               SUM(CASE WHEN finish=1 THEN odds*100 ELSE 0 END) ret_per100
        FROM results
        WHERE substr(date,1,4) BETWEEN '2020' AND '2025'
          AND odds >= 8 AND odds < 11
          AND finish > 0 AND finish < 90
          AND (race_name LIKE '%2勝%' OR race_name LIKE '%２勝%' OR race_name LIKE '%1000万%')
    """
    r = conn.execute(q).fetchone()
    cost = r["n"] * 100
    ret  = r["ret_per100"] or 0
    print(f"  8-11倍 全馬買い: {r['n']}頭 勝={r['w']} ROI={ret/cost*100 if cost else 0:.1f}% 損益{ret-cost:+,}")
    for lo, hi in [(5,8),(8,11),(11,15),(15,20),(20,30)]:
        q2 = f"""
            SELECT COUNT(*) n, SUM(CASE WHEN finish=1 THEN 1 ELSE 0 END) w,
                   SUM(CASE WHEN finish=1 THEN odds*100 ELSE 0 END) r
            FROM results
            WHERE substr(date,1,4) BETWEEN '2020' AND '2025'
              AND odds >= {lo} AND odds < {hi} AND finish > 0 AND finish < 90
              AND (race_name LIKE '%2勝%' OR race_name LIKE '%２勝%' OR race_name LIKE '%1000万%')
        """
        x = conn.execute(q2).fetchone()
        c = x["n"]*100
        rr = x["r"] or 0
        if c:
            print(f"  {lo:>2}-{hi:<2}倍 全馬買: {x['n']:>5} 勝{x['w']:>4} ROI={rr/c*100:>5.1f}% {rr-c:+,}")

    conn.close()


if __name__ == "__main__":
    main()
