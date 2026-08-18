"""repair_garbled_encoding_2026.py
2026年にnar.netkeiba.comがEUC-JP→UTF-8へ配信エンコーディングを切り替えたため、
旧デコードロジック(euc-jp固定)で取得された nar_results.horse_name/jockey/stable が
文字化け(U+FFFD)した既存行を、修正済みの fetch_race_data() で再取得して修復する。

対象: 2026年, venue 42/43/44/45, race_id LIKE '2026%' で horse_name に U+FFFD を含む行
方針: 旧値がgarbled かつ 新値がclean の場合のみUPDATE。それ以外はスキップ。
"""
import sys, time, sqlite3
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, str(Path(__file__).parent))
from build_nar_db_fast import fetch_race_data  # noqa: E402

PROJ = Path(__file__).parent.parent
DB_PATH = PROJ / 'nar_keiba.db'
BACKUP_PATH = Path(r"C:\Users\westr\AppData\Local\Temp\claude\C--Users-westr-norishiko-ai\09819c8c-d505-40aa-a15d-8d1bf1ad0ff6\scratchpad\nar_keiba_backup_before_encoding_repair.db")

REPL = '\ufffd'
SLEEP = 0.5
MAX_CONSEC_FAIL = 8  # IPブロック疑いで即中断する閾値


def main():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute('PRAGMA busy_timeout=30000')

    print(f"[backup] {DB_PATH} -> {BACKUP_PATH}")
    BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    bconn = sqlite3.connect(str(BACKUP_PATH))
    conn.backup(bconn)
    bconn.close()
    print("[backup] done")

    rows = conn.execute("""
        SELECT DISTINCT race_id FROM nar_results
        WHERE race_id LIKE '2026%' AND horse_name LIKE '%' || X'EFBFBD' || '%'
        ORDER BY race_id
    """).fetchall()
    race_ids = [r[0] for r in rows]
    total = len(race_ids)
    print(f"[scope] {total} race_ids to repair")

    updated_rows = 0
    updated_races = 0
    skipped_still_garbled = 0
    fetch_failed = 0
    consec_fail = 0
    per_venue_month = {}

    for i, race_id in enumerate(race_ids):
        vm = race_id[4:6] + race_id[6:8]
        data = fetch_race_data(race_id)
        time.sleep(SLEEP)

        if not data or not data.get('results'):
            fetch_failed += 1
            consec_fail += 1
            print(f"[{i+1}/{total}] {race_id} FETCH_FAILED (consec={consec_fail})")
            if consec_fail >= MAX_CONSEC_FAIL:
                print(f"[ABORT] {consec_fail}連続失敗。IPブロックの疑いあり、即中断。")
                break
            continue
        consec_fail = 0

        cur_rows = conn.execute(
            "SELECT umaban, horse_name, jockey, stable FROM nar_results WHERE race_id=?",
            (race_id,)
        ).fetchall()
        cur_by_umaban = {r[0]: (r[1], r[2], r[3]) for r in cur_rows}

        race_updated = 0
        for res in data['results']:
            umaban = res['umaban']
            if umaban not in cur_by_umaban:
                continue
            old_hn, old_jk, old_st = cur_by_umaban[umaban]
            new_hn, new_jk, new_st = res['horse_name'], res['jockey'], res['stable']

            old_garbled = any(REPL in (v or '') for v in (old_hn, old_jk, old_st))
            new_garbled = any(REPL in (v or '') for v in (new_hn, new_jk, new_st))

            if not old_garbled:
                continue
            if new_garbled:
                skipped_still_garbled += 1
                continue

            conn.execute(
                "UPDATE nar_results SET horse_name=?, jockey=?, stable=? WHERE race_id=? AND umaban=?",
                (new_hn, new_jk, new_st, race_id, umaban)
            )
            race_updated += 1
            updated_rows += 1

        if race_updated:
            updated_races += 1
            conn.commit()
            per_venue_month[vm] = per_venue_month.get(vm, 0) + race_updated

        print(f"[{i+1}/{total}] {race_id} updated={race_updated}")

    conn.commit()

    remaining = conn.execute("""
        SELECT COUNT(*) FROM nar_results
        WHERE race_id LIKE '2026%' AND horse_name LIKE '%' || X'EFBFBD' || '%'
    """).fetchone()[0]

    print("\n=== SUMMARY ===")
    print(f"target races: {total}, processed: {i+1 if total else 0}")
    print(f"updated_races: {updated_races}, updated_rows: {updated_rows}")
    print(f"skipped_still_garbled: {skipped_still_garbled}, fetch_failed: {fetch_failed}")
    print(f"remaining garbled rows (all 2026): {remaining}")
    print("per venue+month updated rows:", per_venue_month)
    conn.close()


if __name__ == '__main__':
    main()
