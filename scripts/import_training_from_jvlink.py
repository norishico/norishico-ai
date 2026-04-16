"""JV-Link 取得済み調教データ (jvlink_dump_training.json) → training テーブル投入

jvlink_fetch.py が SLOP (HC=坂路) + WOOD (WC=ウッドチップ) dataspec で
取得した調教レコードをパース済みJSONから読み込み、training テーブルへ INSERT。

既存 import_training_from_tfjv.py は TFJV DAT (C:\\TFJV\\CK_DATA) を読む、
本スクリプトは JV-Link 直接取得の JSON を読む。
TFJV 起動依存が無くなり完全自動化可能。

horse_id 変換: JV-Link 10桁 ketto_num → netkeiba 8桁 (hid[2:] 世紀prefix除去)
"""
import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import sys

PROJ = pathlib.Path(__file__).resolve().parent.parent
DB_PATH = PROJ / "keiba.db"
JSON_PATH = PROJ / "jvlink_dump_training.json"
LOG = PROJ / "logs" / f"jvlink_training_import_{dt.datetime.now():%Y%m%d_%H%M%S}.log"


def log(msg):
    print(msg, flush=True)
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=str, default=str(JSON_PATH))
    ap.add_argument("--db", type=str, default=str(DB_PATH))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    log(f"[jvlink_training_import] start {dt.datetime.now().isoformat()}")

    json_path = pathlib.Path(args.json)
    if not json_path.exists():
        log(f"  JSON not found: {json_path} (scheduled fetch may not have run yet)")
        return 0

    try:
        records = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"  failed to load JSON: {e}")
        return 1
    log(f"  loaded {len(records)} records from {json_path.name}")

    if not records:
        log("  no records to import")
        return 0

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")

    log("  building horse_id → horse_name cache from results...")
    hid_to_name = {}
    for hid, name in conn.execute(
        "SELECT DISTINCT horse_id, horse_name FROM results "
        "WHERE horse_id IS NOT NULL AND horse_id != ''"
    ):
        if hid and hid not in hid_to_name:
            hid_to_name[hid] = (name or "").strip()
    log(f"  cache size: {len(hid_to_name):,} horses")

    cur = conn.cursor()
    inserted = 0
    unknown = 0
    errors = 0
    for rec in records:
        try:
            # TFJV 10-char → netkeiba 8-char
            raw_hid = rec.get("horse_id", "")
            hid_key = raw_hid[2:] if len(raw_hid) == 10 else raw_hid
            horse_name = hid_to_name.get(hid_key) or hid_to_name.get(raw_hid)
            if not horse_name:
                unknown += 1
                continue
            if args.dry_run:
                continue
            r = cur.execute(
                "INSERT OR IGNORE INTO training (horse_name, date, venue, source, lap1, lap2) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (horse_name, rec["date"], rec["venue"], rec["source"],
                 rec["lap1"], rec["lap2"]),
            )
            if r.rowcount:
                inserted += 1
        except Exception as e:
            errors += 1
            if errors <= 3:
                log(f"  insert error: {e} on {rec}")

    if not args.dry_run:
        conn.commit()

    new_max = conn.execute("SELECT MAX(date) FROM training").fetchone()[0]
    log(f"  summary: records={len(records)} inserted={inserted} "
        f"unknown={unknown} errors={errors}")
    log(f"  training MAX date now: {new_max}")
    conn.close()
    log(f"[jvlink_training_import] done {dt.datetime.now().isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
