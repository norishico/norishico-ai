# -*- coding: utf-8 -*-
"""
build_course_slope.py — venue_elevation.md(2026-07-20調査)の最終直線の坂データを
course_slopeテーブルとして構造化しkeiba.dbに投入する(新規追加テーブル、既存テーブルへの
書き込みなし)。

対象は「上がり3F区間(残り600m以内)に定量化された坂がある」と確認済みの5場のみ
(東京・中山・中京・阪神・小倉)。他5場(札幌・函館・福島・新潟・京都)は最終直線の
定量データが確認できなかったため収録しない(=平坦として扱う、venue_elevation.mdの
確度表記に準拠)。

芝/ダートは同じ地形を共有すると仮定し(小倉のみダート実測値を芝にも暫定適用、
venue_elevation.md自身の注記通り)、両surfaceに同一の坂データを適用する。
"""
import sqlite3

DB_PATH = "keiba.db"

# (venue, remaining_start_m, remaining_end_m, height_change_m, source_note, confidence)
# remaining_start_m > remaining_end_m (ゴールに向かうほど残り距離は減る)
# height_change_m: 正=上り、負=下り
SLOPE_DATA = [
    ("東京", 460.0, 300.0, 2.0,
     "JRA公式+検索一致。上がり3F区間の坂の高さとして2.0mを採用(コース全体高低差2.7mとは別数値)",
     "medium"),
    ("中山", 180.0, 70.0, 2.2,
     "JRA公式・Wikipedia・検索一致(最大勾配2.24%、JRA10場中最大の坂)",
     "high"),
    ("中京", 340.0, 240.0, 2.0,
     "JRA公式引用含む2ソース一致(勾配2.0%、ラスト240mは平坦)",
     "medium"),
    ("阪神", 200.0, 90.0, 1.9,
     "急坂、坂長110m。ソースにより1.8-2.4mの幅があり中央値1.9mを採用(外回りコースの数値)",
     "low_range"),
    ("小倉", 400.0, 0.0, 0.6,
     "ダートコースで確認された数値を芝にも暫定適用(芝の直接数値は未確認、坂の終了地点も"
     "明記なしのためゴールまで緩やかに続くと仮定)",
     "low_estimate"),
]


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS course_slope (
            venue TEXT NOT NULL,
            surface TEXT NOT NULL,
            venue_variant TEXT NOT NULL DEFAULT 'default',
            remaining_start_m REAL NOT NULL,
            remaining_end_m REAL NOT NULL,
            height_change_m REAL NOT NULL,
            grade REAL NOT NULL,
            source TEXT,
            confidence TEXT,
            PRIMARY KEY (venue, surface, venue_variant, remaining_start_m)
        )
    """)
    conn.execute("DELETE FROM course_slope")

    rows = []
    for venue, r_start, r_end, height, source, confidence in SLOPE_DATA:
        grade = height / (r_start - r_end)
        for surface in ("芝", "ダ"):
            rows.append((venue, surface, "default", r_start, r_end, height, grade, source, confidence))

    conn.executemany("""
        INSERT INTO course_slope
        (venue, surface, venue_variant, remaining_start_m, remaining_end_m,
         height_change_m, grade, source, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()

    print(f"投入件数: {len(rows)}件({len(SLOPE_DATA)}場 x 芝/ダ2surface)")
    for row in conn.execute("SELECT venue, surface, remaining_start_m, remaining_end_m, height_change_m, grade, confidence FROM course_slope ORDER BY venue, surface"):
        print(row)
    conn.close()


if __name__ == "__main__":
    main()
