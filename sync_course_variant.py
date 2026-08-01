# -*- coding: utf-8 -*-
"""
sync_course_variant.py — jvlink_dump.json(jvlink_fetch.py出力)のraレコードから
course_kubun(開催の仮設ラチ位置区分、RA構造体offset710より取得済みだが従来は未使用の
まま捨てられていた)を抽出し、race_course_variantテーブルに投入する。
【2026-08-01 JV-Data仕様書_4.9.0.1.xlsxで正式確認】半角2文字、"A "〜"E "の5区分
(2002年以前の東京競馬場は"A1"/"A2"も存在)。「A/B/C/D」の4区分ではない(要訂正済み)。

【設計判断】course_kubunをresults/dividendsテーブルに直接カラム追加せず、独立した新規
テーブルとする理由: build_db.pyのupsert_df()はINSERT OR REPLACEでUPSERTするため、
一部カラムしか持たないDataFrameで更新すると、そのレコードの他のカラム(margin/
prev_popularity/jockey_change等、別処理で埋められる値)がNULLに巻き戻されるリスクが
ある。race_id + course_kubunのみを持つ独立テーブルにすることでこのリスクを完全に
回避する(course_layout/course_start_layout/course_slopeと同じ設計方針)。

【JV-Link取得の制約・重要】JVOpen("RACE", fromtime, 1)のセットアップデータは、
fromtimeに関わらず直近約1年分のローリングウィンドウしか返さない(2026-07時点で確認
済み)。そのため本テーブルは今後の日次fetch_and_build.py実行で新規レース分から
蓄積されていくのみで、過去(2025-08以前)分の全量取得は不可能。

使い方:
    python sync_course_variant.py [jvlink_dump.jsonのパス]
"""
import sys
import json
import sqlite3
import datetime as dt

from jvlink_fetch import JYO_NAME

DB_PATH = "keiba.db"
DEFAULT_DUMP_PATH = "jvlink_dump.json"


def build_race_id(ra):
    """ra辞書からresults.race_idと同じ'YYYY-MM-DD_venue_racenum'形式を組み立てる
    (build_db.py:parse_date_ymd/race_id構築ロジックと同一の規則で独立に再実装)。"""
    year = ra.get("year", "")
    monthday = ra.get("monthday", "")
    jyo = ra.get("jyo", "")
    race_num = ra.get("race_num", "")
    if len(year) != 4 or len(monthday) != 4 or jyo not in JYO_NAME:
        return None
    try:
        y, m, d = int(year), int(monthday[:2]), int(monthday[2:])
        date = f"{y:04d}-{m:02d}-{d:02d}"
        rn = str(int(race_num))
    except ValueError:
        return None
    return f"{date}_{JYO_NAME[jyo]}_{rn}"


def sync(dump_path=DEFAULT_DUMP_PATH, db_path=DB_PATH):
    with open(dump_path, encoding="utf-8") as f:
        dump = json.load(f)
    ra_records = dump.get("ra", [])

    rows = []
    n_skipped = 0
    for ra in ra_records:
        race_id = build_race_id(ra)
        course_kubun = (ra.get("course_kubun") or "").strip()
        if race_id is None or not course_kubun:
            n_skipped += 1
            continue
        rows.append((race_id, course_kubun))

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS race_course_variant (
            race_id TEXT PRIMARY KEY,
            course_kubun TEXT,
            fetched_at TEXT,
            source TEXT
        )
    """)
    fetched_at = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany("""
        INSERT INTO race_course_variant (race_id, course_kubun, fetched_at, source)
        VALUES (?, ?, ?, 'jvlink_dump')
        ON CONFLICT(race_id) DO UPDATE SET
            course_kubun=excluded.course_kubun,
            fetched_at=excluded.fetched_at,
            source=excluded.source
    """, [(rid, ck, fetched_at) for rid, ck in rows])
    conn.commit()

    n_total = conn.execute("SELECT COUNT(*) FROM race_course_variant").fetchone()[0]
    conn.close()

    print(f"raレコード数: {len(ra_records)}  投入: {len(rows)}件  "
          f"スキップ(race_id不明/course_kubun空): {n_skipped}件")
    print(f"race_course_variant 総件数: {n_total}")


if __name__ == "__main__":
    dump_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DUMP_PATH
    sync(dump_path)
