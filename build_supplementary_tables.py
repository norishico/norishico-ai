"""
ノリシコ競馬AI / build_supplementary_tables.py
results テーブルから補助テーブルを生成する

生成テーブル:
  1. bloodline_stats       : 父・母父 × コース × 距離別スコア
  2. gate_cond_blood_bonus : 枠順 × 馬場 × 血統の加点テーブル
  3. track_bias_bonus      : 開催週フェーズ × 枠 × 脚質の加点テーブル
  4. race_pace             : レースPCI（ペース指数）

使い方:
    python build_supplementary_tables.py
    python build_supplementary_tables.py --only bloodline  # 個別実行
"""

import sqlite3
import numpy as np
import sys
from pathlib import Path

DB_PATH = "keiba.db"

def get_conn(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ══════════════════════════════════════════════════════════════
# 1. bloodline_stats（父・母父 × 面 × 距離バケット → スコア）
# ══════════════════════════════════════════════════════════════

def build_bloodline_stats(conn, cutoff_date=None):
    print(f"  🔄 bloodline_stats 生成中...{f' (cutoff={cutoff_date})' if cutoff_date else ''}")

    conn.execute("DROP TABLE IF EXISTS bloodline_stats")
    conn.execute("""
        CREATE TABLE bloodline_stats (
            col_type    TEXT,   -- 'sire' or 'dam_sire'
            name        TEXT,
            surface     TEXT,
            dist_bucket INTEGER,
            n           INTEGER,
            wins        INTEGER,
            top3        INTEGER,
            win_rate    REAL,
            top3_rate   REAL,
            score       REAL,
            PRIMARY KEY (col_type, name, surface, dist_bucket)
        )
    """)

    date_filter = f"AND date < '{cutoff_date}'" if cutoff_date else ""

    # 全体平均勝率（ノーマライズ用）
    avg_wr = conn.execute(f"""
        SELECT AVG(wr) FROM (
            SELECT 1.0*SUM(CASE WHEN finish=1 THEN 1 ELSE 0 END)/COUNT(*) as wr
            FROM results WHERE finish<90 {date_filter} GROUP BY race_id
        )
    """).fetchone()[0] or 0.0769

    def _insert_bloodline(col_type: str, col_name: str):
        rows = conn.execute(f"""
            SELECT {col_name} as name,
                   surface,
                   (distance/400)*400 as dist_bucket,
                   COUNT(*) as n,
                   SUM(CASE WHEN finish=1 THEN 1.0 ELSE 0 END) as wins,
                   SUM(CASE WHEN finish<=3 THEN 1.0 ELSE 0 END) as top3
            FROM results
            WHERE finish<90 AND finish IS NOT NULL
              AND {col_name} IS NOT NULL AND {col_name} != ''
              {date_filter}
            GROUP BY {col_name}, surface, dist_bucket
            HAVING n >= 10
        """).fetchall()

        inserted = 0
        for r in rows:
            wr = r['wins'] / r['n']
            t3r = r['top3'] / r['n']
            # スコア計算: 平均勝率を60点として正規化
            # wr/avg_wr * 55 + 20 → 平均≈60、上位≈90+
            raw_score = wr / avg_wr * 55 + 20
            # サンプル少ない場合は中間値に引き寄せ
            if r['n'] < 30:
                raw_score = raw_score * (r['n'] / 30) + 55 * (1 - r['n'] / 30)
            score = min(100.0, max(0.0, raw_score))

            conn.execute("""
                INSERT OR REPLACE INTO bloodline_stats
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (col_type, r['name'], r['surface'], r['dist_bucket'],
                  r['n'], int(r['wins']), int(r['top3']),
                  round(wr, 4), round(t3r, 4), round(score, 1)))
            inserted += 1

        return inserted

    n_sire = _insert_bloodline('sire', 'sire')
    n_dam  = _insert_bloodline('dam_sire', 'dam_sire')

    conn.execute("CREATE INDEX IF NOT EXISTS idx_blood_lookup ON bloodline_stats(col_type, name, surface, dist_bucket)")
    conn.commit()
    print(f"     ✅ sire={n_sire:,}件, dam_sire={n_dam:,}件")


# ══════════════════════════════════════════════════════════════
# 2. gate_cond_blood_bonus（枠×馬場×血統の加点）
# ══════════════════════════════════════════════════════════════

def build_gate_cond_blood_bonus(conn, cutoff_date=None):
    print(f"  🔄 gate_cond_blood_bonus 生成中...{f' (cutoff={cutoff_date})' if cutoff_date else ''}")

    conn.execute("DROP TABLE IF EXISTS gate_cond_blood_bonus")
    conn.execute("""
        CREATE TABLE gate_cond_blood_bonus (
            venue       TEXT,
            surface     TEXT,
            distance    INTEGER,
            gate_cat    TEXT,   -- '内枠'|'中枠'|'外枠'|'全枠'
            track_cond  TEXT,   -- '良'|'稍重'|'重'|'不良'|'全'
            sire        TEXT,
            n           INTEGER,
            win_rate    REAL,
            avg_win_rate REAL,
            diff        REAL,   -- 平均との差分（%pt）
            bonus       REAL,   -- スコア加点値
            PRIMARY KEY (venue, surface, distance, gate_cat, track_cond, sire)
        )
    """)

    date_filter = f"AND date < '{cutoff_date}'" if cutoff_date else ""

    # 全体平均勝率（会場×面×距離）
    avg_map = {}
    rows = conn.execute(f"""
        SELECT venue, surface, distance,
               AVG(CASE WHEN finish=1 THEN 1.0 ELSE 0 END)*100 as avg_wr
        FROM results WHERE finish<90 AND finish IS NOT NULL {date_filter}
        GROUP BY venue, surface, distance
    """).fetchall()
    for r in rows:
        avg_map[(r['venue'], r['surface'], r['distance'])] = r['avg_wr']

    inserted = 0
    # 枠×血統×コース
    combos = [
        ('gate_cat', """
            CASE WHEN CAST(horse_num AS REAL)/num_horses <= 0.35 THEN '内枠'
                 WHEN CAST(horse_num AS REAL)/num_horses >= 0.65 THEN '外枠'
                 ELSE '中枠' END
        """),
        ('all_gate', "'全枠'"),
    ]
    cond_groups = [
        ('全', "1=1"),
        ('cond', "track_cond IN ('稍重','重','不良')"),
    ]

    for gate_label, gate_expr in combos:
        for cond_label, cond_filter in cond_groups:
            rows = conn.execute(f"""
                SELECT venue, surface, distance,
                       {gate_expr} as gate_cat,
                       CASE WHEN {cond_filter} THEN
                           CASE WHEN track_cond IN ('稍重','重','不良') THEN track_cond ELSE '全' END
                       ELSE '全' END as tc,
                       sire,
                       COUNT(*) as n,
                       AVG(CASE WHEN finish=1 THEN 1.0 ELSE 0 END)*100 as wr
                FROM results
                WHERE finish<90 AND finish IS NOT NULL
                  AND sire IS NOT NULL AND sire != ''
                  AND num_horses > 0 AND horse_num > 0
                  AND ({cond_filter})
                  {date_filter}
                GROUP BY venue, surface, distance, gate_cat, tc, sire
                HAVING n >= 8
            """).fetchall()

            for r in rows:
                avg_wr = avg_map.get((r['venue'], r['surface'], r['distance']), 7.0)
                diff = r['wr'] - avg_wr
                # diff > +5ptのみ加点（ノイズ除去）
                if diff < 5.0:
                    continue
                bonus = min(8.0, diff * 0.4)  # 最大8点

                conn.execute("""
                    INSERT OR REPLACE INTO gate_cond_blood_bonus
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (r['venue'], r['surface'], r['distance'],
                      r['gate_cat'], r['tc'], r['sire'],
                      r['n'], round(r['wr'], 2), round(avg_wr, 2),
                      round(diff, 2), round(bonus, 2)))
                inserted += 1

    conn.execute("CREATE INDEX IF NOT EXISTS idx_gcbb_lookup ON gate_cond_blood_bonus(venue, surface, distance, gate_cat, track_cond, sire)")
    conn.commit()
    print(f"     ✅ {inserted:,}件")


# ══════════════════════════════════════════════════════════════
# 3. track_bias_bonus（開催週フェーズ × 枠 × 脚質）
# ══════════════════════════════════════════════════════════════

def build_track_bias_bonus(conn, cutoff_date=None):
    print(f"  🔄 track_bias_bonus 生成中...{f' (cutoff={cutoff_date})' if cutoff_date else ''}")

    conn.execute("DROP TABLE IF EXISTS track_bias_bonus")
    conn.execute("""
        CREATE TABLE track_bias_bonus (
            venue       TEXT,
            surface     TEXT,
            phase       TEXT,   -- '前半'|'中盤'|'後半'
            gate_cat    TEXT,   -- '内枠'|'中枠'|'外枠'
            style_cat   TEXT,   -- '先団'|'中団'|'後方'
            n           INTEGER,
            win_rate    REAL,
            avg_win_rate REAL,
            diff        REAL,
            bonus       REAL,
            PRIMARY KEY (venue, surface, phase, gate_cat, style_cat)
        )
    """)

    from datetime import datetime, timedelta
    from collections import defaultdict

    date_filter = f"AND date < '{cutoff_date}'" if cutoff_date else ""

    # 開催日を週番号に変換（会場単位でセッションを特定）
    all_dates = conn.execute(f"""
        SELECT DISTINCT venue, date FROM results
        WHERE finish<90 {date_filter} ORDER BY venue, date
    """).fetchall()

    # 会場別に週番号マップを構築
    venue_dates = defaultdict(list)
    for r in all_dates:
        venue_dates[r['venue']].append(r['date'])

    week_map = {}  # (venue, date) → week_num
    for venue, dates in venue_dates.items():
        dates = sorted(set(dates))
        session_start = dates[0]
        wn = 1
        prev_d = None
        for d in dates:
            if prev_d:
                gap = (datetime.strptime(d, '%Y-%m-%d') -
                       datetime.strptime(prev_d, '%Y-%m-%d')).days
                if gap > 21:
                    session_start = d
                    wn = 1
                elif gap >= 6:
                    wn += 1
            week_map[(venue, d)] = wn
            prev_d = d

    # resultsに週番号を付与して集計
    rows = conn.execute(f"""
        SELECT venue, surface, date,
               CAST(horse_num AS REAL)/num_horses as gate_ratio,
               CAST(pos4 AS REAL)/num_horses as style_ratio,
               finish, num_horses
        FROM results
        WHERE finish<90 AND finish IS NOT NULL
          AND pos4 > 0 AND num_horses > 0 AND horse_num > 0
          {date_filter}
    """).fetchall()

    # (venue, surface, phase, gate_cat, style_cat) → [finish, num_horses]
    data = defaultdict(list)
    for r in rows:
        wn = week_map.get((r['venue'], r['date']), 1)
        phase = '前半' if wn <= 3 else ('中盤' if wn <= 5 else '後半')
        gate  = ('内枠' if r['gate_ratio'] <= 0.35 else
                 ('外枠' if r['gate_ratio'] >= 0.65 else '中枠'))
        style = ('先団' if r['style_ratio'] <= 0.33 else
                 ('後方' if r['style_ratio'] >= 0.67 else '中団'))
        key = (r['venue'], r['surface'], phase, gate, style)
        data[key].append((r['finish'], r['num_horses']))

    # 全体平均（venue×surface）
    avg_wr_map = defaultdict(list)
    for (venue, surface, phase, gate, style), results_list in data.items():
        wins = sum(1 for f, nh in results_list if f == 1)
        avg_wr_map[(venue, surface)].append(wins / len(results_list) * 100)

    global_avg = {}
    for (venue, surface), wrs in avg_wr_map.items():
        global_avg[(venue, surface)] = np.mean(wrs)

    inserted = 0
    for (venue, surface, phase, gate, style), results_list in data.items():
        if len(results_list) < 30:
            continue
        wins = sum(1 for f, nh in results_list if f == 1)
        wr   = wins / len(results_list) * 100
        avg  = global_avg.get((venue, surface), 7.0)
        diff = wr - avg
        # diff/3 で加点（±5点上限）
        bonus = max(-5.0, min(5.0, diff / 3))
        if abs(bonus) < 0.5:  # 軽微な差は無視
            continue

        conn.execute("""
            INSERT OR REPLACE INTO track_bias_bonus
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (venue, surface, phase, gate, style,
              len(results_list), round(wr, 2), round(avg, 2),
              round(diff, 2), round(bonus, 2)))
        inserted += 1

    conn.execute("CREATE INDEX IF NOT EXISTS idx_tbb_lookup ON track_bias_bonus(venue, surface, phase, gate_cat, style_cat)")
    conn.commit()
    print(f"     ✅ {inserted:,}件")


# ══════════════════════════════════════════════════════════════
# 4. race_pace（PCI: ペース指数）
# ══════════════════════════════════════════════════════════════

def build_race_pace(conn):
    """
    PCI（Pace Change Index）を推定してrace_paceテーブルを生成。
    pos1とpos4の変動から前後半のペースバランスを推定。

    PCI = 前半に位置が上がった馬の比率 × 100
      < 48 → ハイペース（前崩れ）
      48-52 → 標準
      > 52 → スローペース
    """
    print("  🔄 race_pace 生成中...")

    conn.execute("DROP TABLE IF EXISTS race_pace")
    conn.execute("""
        CREATE TABLE race_pace (
            race_id  TEXT PRIMARY KEY,
            date     TEXT,
            venue    TEXT,
            race_num INTEGER,
            surface  TEXT,
            distance INTEGER,
            pci      REAL,
            n_horses INTEGER
        )
    """)

    # race単位で集計
    rows = conn.execute("""
        SELECT race_id, date, venue, race_num, surface, distance,
               COUNT(*) as n,
               AVG(CASE WHEN pos1 > 0 AND pos4 > 0
                        AND CAST(pos4 AS REAL) > CAST(pos1 AS REAL)
                        THEN 1.0 ELSE 0.0 END) * 100 as pci_raw
        FROM results
        WHERE finish<90 AND pos1>0 AND pos4>0 AND num_horses>0
        GROUP BY race_id
        HAVING n >= 5
    """).fetchall()

    inserted = 0
    for r in rows:
        # pci_raw: 後方から前に進出した馬の比率（高い=スロー）
        # 反転: スローペース = 後続が前に行ける = pci_raw高い
        # ハイペース = 逃げ先行が失速 = pos4 < pos1 が多い = pci_raw低い
        pci = 100 - r['pci_raw']  # 低い方がハイペースになるよう変換

        conn.execute("""
            INSERT OR REPLACE INTO race_pace VALUES (?,?,?,?,?,?,?,?)
        """, (r['race_id'], r['date'], r['venue'], r['race_num'],
              r['surface'], r['distance'], round(pci, 1), r['n']))
        inserted += 1

    conn.execute("CREATE INDEX IF NOT EXISTS idx_rp_venue ON race_pace(venue, surface, distance, date)")
    conn.commit()
    print(f"     ✅ {inserted:,}件")


# ══════════════════════════════════════════════════════════════
# 5. prev_finish / prev_distance を results に追記
# ══════════════════════════════════════════════════════════════

def build_prev_run_cols(conn):
    """
    resultsテーブルに前走情報（prev_finish, prev_distance）を追記。
    同一馬の直前レースを自己JOINで取得。
    """
    print("  🔄 prev_finish / prev_distance 追記中...")

    # カラム追加
    for col, dtype in [('prev_finish', 'REAL'), ('prev_distance', 'INTEGER'),
                       ('prev_venue', 'TEXT'), ('prev_surface', 'TEXT')]:
        try:
            conn.execute(f"ALTER TABLE results ADD COLUMN {col} {dtype}")
        except:
            pass  # 既存カラムはスキップ

    # 前走データを一括計算
    updated = conn.execute("""
        UPDATE results
        SET prev_finish   = (
            SELECT r2.finish FROM results r2
            WHERE r2.horse_name = results.horse_name
              AND r2.date < results.date
              AND r2.finish < 90
            ORDER BY r2.date DESC LIMIT 1
        ),
        prev_distance = (
            SELECT r2.distance FROM results r2
            WHERE r2.horse_name = results.horse_name
              AND r2.date < results.date
              AND r2.finish < 90
            ORDER BY r2.date DESC LIMIT 1
        ),
        prev_venue = (
            SELECT r2.venue FROM results r2
            WHERE r2.horse_name = results.horse_name
              AND r2.date < results.date
              AND r2.finish < 90
            ORDER BY r2.date DESC LIMIT 1
        ),
        prev_surface = (
            SELECT r2.surface FROM results r2
            WHERE r2.horse_name = results.horse_name
              AND r2.date < results.date
              AND r2.finish < 90
            ORDER BY r2.date DESC LIMIT 1
        )
        WHERE prev_finish IS NULL
    """).rowcount
    conn.commit()
    print(f"     ✅ {updated:,}件更新")


# ══════════════════════════════════════════════════════════════
# メイン
# ══════════════════════════════════════════════════════════════

def build_all(db_path=DB_PATH, only=None, cutoff_date=None):
    print(f"\n{'═'*60}")
    print(f"  ノリシコ競馬AI — 補助テーブル生成{f'  cutoff={cutoff_date}' if cutoff_date else ''}")
    print(f"{'═'*60}\n")

    conn = get_conn(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-131072")  # 128MB

    # cutoff_date対応の関数と非対応の関数を分離
    steps_with_cutoff = {
        'bloodline':  build_bloodline_stats,
        'gate':       build_gate_cond_blood_bonus,
        'track_bias': build_track_bias_bonus,
    }
    steps_no_cutoff = {
        'race_pace':  build_race_pace,
        'prev_run':   build_prev_run_cols,
    }

    for name, func in {**steps_with_cutoff, **steps_no_cutoff}.items():
        if only and name != only:
            continue
        try:
            if name in steps_with_cutoff and cutoff_date:
                func(conn, cutoff_date=cutoff_date)
            else:
                func(conn)
        except Exception as e:
            import traceback
            print(f"     ❌ {name} エラー: {e}")
            traceback.print_exc()

    # 確認
    print(f"\n{'─'*60}")
    for tbl in ['bloodline_stats', 'gate_cond_blood_bonus', 'track_bias_bonus', 'race_pace']:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            print(f"  {tbl}: {n:,}件")
        except:
            print(f"  {tbl}: テーブルなし")

    print(f"\n  保存先: {Path(db_path).absolute()}")
    print(f"{'═'*60}\n")
    conn.close()


if __name__ == '__main__':
    only = None
    if '--only' in sys.argv:
        idx = sys.argv.index('--only')
        only = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
    build_all(only=only)
