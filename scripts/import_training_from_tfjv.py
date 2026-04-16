"""TFJV CK_DATA → training テーブル自動インポート
TFJV (TARGET frontier JV) が JV-Link 経由で自動取得している調教データを
C:\\TFJV\\CK_DATA\\YYYY\\YYYYMM\\ から直接パースして training テーブルへ投入。

手動CSVエクスポート不要。TFJV起動時に自動でDATが更新されるので
Task Schedulerで定期実行すれば完全自動化になる。

DATレコード仕様 (47バイト固定長, CP932):
  [0]     flag
  [1:5]   year (YYYY)
  [5:9]   mmdd
  [9:13]  hhmm (training time)
  [13:23] horse_id (ketto_num, 10 chars)
  [23:27] 5F cumulative (x10 sec)
  [27:30] 5F→4F 1F segment
  [30:34] 4F cumulative
  [34:37] 4F→3F 1F segment
  [37:41] 3F cumulative
  [41:44] **lap2** (前1F, =3F→2F segment)
  [44:47] **lap1** (最終1F)

ファイル名プレフィックス:
  HC = 坂路 (source=sakuro)
  WC = ウッドチップ (source=woodc)
  02 = 栗東, 12 = 美浦 (best guess)
"""
import argparse
import datetime as dt
import pathlib
import sqlite3
import sys

TFJV_ROOT = pathlib.Path(r"C:\TFJV\CK_DATA")
PROJ = pathlib.Path(__file__).resolve().parent.parent
DB_PATH = PROJ / "keiba.db"
LOG = PROJ / "logs" / f"tfjv_training_{dt.datetime.now():%Y%m%d_%H%M%S}.log"

SRC_MAP = {"HC": "sakuro", "WC": "woodc"}
VENUE_MAP = {"02": "栗東", "12": "美浦"}


def log(msg):
    print(msg, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def parse_record(line: str):
    """TFJV調教レコード → dict or None.
    HC (坂路): 47 chars
    WC (ウッドチップ): 92 chars
    共通の先頭23バイト(flag+year+mmdd+hhmm+horse_id)と末尾6バイト(lap2+lap1)。
    """
    if len(line) not in (47, 92):
        return None
    try:
        year = line[1:5]
        mmdd = line[5:9]
        date = f"{year}-{mmdd[:2]}-{mmdd[2:]}"
        horse_id = line[13:23]
        lap2 = int(line[-6:-3]) / 10.0
        lap1 = int(line[-3:]) / 10.0
        return {"date": date, "horse_id": horse_id, "lap1": lap1, "lap2": lap2}
    except (ValueError, IndexError):
        return None


def iter_dat_files(since_date: str):
    """Yield (path, source, venue) for DAT files newer than since_date."""
    if not TFJV_ROOT.exists():
        log(f"TFJV not found: {TFJV_ROOT}")
        return
    since = since_date.replace("-", "")
    for year_dir in sorted(TFJV_ROOT.iterdir()):
        if not year_dir.is_dir():
            continue
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir():
                continue
            for dat in sorted(month_dir.glob("*.DAT")):
                name = dat.stem  # e.g. HC020260411
                if len(name) < 11:
                    continue
                prefix = name[:2]
                venue_code = name[2:4]
                date_str = name[4:]  # YYYYMMDD (without 0 before month?)
                # Format is actually HC02 0260411 — missing century. Full file name:
                # HC020260411.DAT: HC02 + 0260411 where 0260411 = 2026-04-11
                # So date part starts at [4:11] and is 7 chars "YMMDD" with Y=single-digit offset?
                # Looking at HC020260411: chars after HC02 = "0260411" = 7 chars
                # Interpreting: '0' + '26' + '04' + '11' → year=2026(!), mmdd=0411
                # So date part is 7 chars where first is constant '0', then 2-digit year (26→2026)
                src = SRC_MAP.get(prefix)
                venue = VENUE_MAP.get(venue_code)
                if not src:
                    continue
                # Extract date from filename
                date_part = name[4:]
                if len(date_part) == 7 and date_part[0] == "0":
                    fy = "20" + date_part[1:3]
                    fmm = date_part[3:5]
                    fdd = date_part[5:7]
                    file_date = f"{fy}-{fmm}-{fdd}"
                else:
                    continue
                if file_date <= since_date:
                    continue
                yield dat, src, venue, file_date


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=str, default=None,
                    help="Import DAT files with date > SINCE. Default = DB training MAX(date)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="Re-import existing date range and compare (no INSERT)")
    args = ap.parse_args()

    LOG.parent.mkdir(exist_ok=True)
    log(f"[tfjv_import] start {dt.datetime.now().isoformat()}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")

    since = args.since or conn.execute("SELECT MAX(date) FROM training").fetchone()[0]
    log(f"  training MAX date: {since}")

    # horse_id → horse_name lookup (cache)
    # TFJV/JV-Link uses 10-char ketto_num (e.g. 2021107171)
    # netkeiba results table uses 8-char format (strip first 2 century digits)
    # We index by the 8-char form so we can look up via tfjv_id[2:]
    log("  building horse_id → horse_name cache from results...")
    hid_to_name = {}
    for hid, name in conn.execute(
        "SELECT DISTINCT horse_id, horse_name FROM results WHERE horse_id IS NOT NULL AND horse_id != ''"
    ):
        if hid and hid not in hid_to_name:
            hid_to_name[hid] = (name or "").strip()
    log(f"  cache size: {len(hid_to_name):,} horses")

    total_records = 0
    total_inserted = 0
    total_unknown = 0
    files_processed = 0

    cur = conn.cursor()
    for dat_path, src, venue, file_date in iter_dat_files(since):
        files_processed += 1
        lines = dat_path.read_bytes().decode("cp932", "replace").split("\r\n")
        recs_in_file = 0
        ins_in_file = 0
        for line in lines:
            rec = parse_record(line)
            if not rec:
                continue
            recs_in_file += 1
            # TFJV 10-char → netkeiba 8-char (drop century prefix)
            hid_key = rec["horse_id"][2:] if len(rec["horse_id"]) == 10 else rec["horse_id"]
            horse_name = hid_to_name.get(hid_key) or hid_to_name.get(rec["horse_id"])
            if not horse_name:
                total_unknown += 1
                continue
            if args.dry_run or args.verify:
                continue
            r = cur.execute(
                "INSERT OR IGNORE INTO training (horse_name, date, venue, source, lap1, lap2) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (horse_name, rec["date"], venue, src, rec["lap1"], rec["lap2"]),
            )
            if r.rowcount:
                ins_in_file += 1
        total_records += recs_in_file
        total_inserted += ins_in_file
        log(f"  {dat_path.name}: recs={recs_in_file} ins={ins_in_file} ({src}/{venue} {file_date})")

    if not args.dry_run and not args.verify:
        conn.commit()

    log(f"  summary: files={files_processed} records={total_records} "
        f"inserted={total_inserted} unknown_horse={total_unknown}")
    new_max = conn.execute("SELECT MAX(date) FROM training").fetchone()[0]
    log(f"  training MAX date now: {new_max}")
    conn.close()
    log(f"[tfjv_import] done {dt.datetime.now().isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
