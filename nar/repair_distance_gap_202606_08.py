"""repair_distance_gap_202606_08.py - 2026年6-8月にnar.netkeiba.com側パース不良で
distance/race_nameがNULL/0になった705レースを、地方競馬公式サイト(keiba.go.jp)の
月次CSVから復元する。

安全設計:
  - UPDATE対象は distance / race_name の2カラムのみ。class_code・他カラムは一切触らない
  - 実行前に conn.backup() でバックアップ
  - 対象レース(distance IS NULL OR 0、2026年06-08月)以外は一切UPDATEしない

使い方:
  python nar/repair_distance_gap_202606_08.py --csv-root <展開済みCSVの親> --dry-run
  python nar/repair_distance_gap_202606_08.py --csv-root <...> --apply
"""
import argparse
import csv
import sqlite3
import urllib.request
from pathlib import Path

PROJ = Path(__file__).parent.parent
DB_PATH = PROJ / 'nar_keiba.db'

VENUE_CD_TO_NAME = {'42': '浦和', '43': '船橋', '44': '大井', '45': '川崎'}


def download_month(year, month, dest_dir):
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_path = dest_dir / f'{year}{month:02d}_racelist.csv'
    if out_path.exists():
        return out_path
    url = (f"https://www.keiba.go.jp/KeibaWeb/DataDownload/RaceDataDownload"
           f"?type=monthly&k_year={year}&k_month={month}")
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    import io, zipfile
    zf = zipfile.ZipFile(io.BytesIO(data))
    for name in zf.namelist():
        if 'racelist' in name.lower() or name.endswith('_1.csv') or '競走成績' in name:
            pass
    # 実際のZIP内ファイル名を確認して racelist を取り出す
    racelist_name = None
    for name in zf.namelist():
        if 'racelist' in name.lower():
            racelist_name = name
            break
    if racelist_name is None:
        # 拡張子csvで距離列を持つ最初のファイルを探すフォールバック
        candidates = [n for n in zf.namelist() if n.lower().endswith('.csv')]
        raise RuntimeError(f"racelist.csv相当が見つからない: {candidates}")
    with zf.open(racelist_name) as f, open(out_path, 'wb') as out:
        out.write(f.read())
    return out_path


def race_id_of(row):
    cd = {'浦和': '42', '船橋': '43', '大井': '44', '川崎': '45'}.get(row['競馬場'])
    if cd is None:
        return None
    date8 = row['競走年月日']
    return f"{date8[0:4]}{cd}{date8[4:6]}{date8[6:8]}{int(row['レース番号']):02d}"


def load_racelist(path):
    """race_id -> (distance, race_name)"""
    out = {}
    with open(path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            rid = race_id_of(row)
            if rid is None:
                continue
            dist_txt = (row.get('距離') or '').strip()
            distance = int(dist_txt) if dist_txt.isdigit() else None
            race_name = (row.get('レース名') or '').strip()
            out[rid] = (distance, race_name)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv-root', required=True)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--backup-dir', default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    targets = conn.execute("""
        SELECT race_id FROM nar_races
        WHERE race_id LIKE '2026%' AND (distance IS NULL OR distance=0)
          AND SUBSTR(race_id,7,2) IN ('06','07','08')
    """).fetchall()
    target_ids = {r['race_id'] for r in targets}
    print(f"[対象] {len(target_ids)}件")

    csv_root = Path(args.csv_root)
    lookup = {}
    for y, m in [(2026, 6), (2026, 7), (2026, 8)]:
        month_dir = csv_root / f'race_{y}_{m}'
        existing = list(month_dir.glob('*_racelist.csv')) if month_dir.exists() else []
        if existing:
            path = existing[0]
        else:
            print(f"[download] {y}-{m:02d} racelist.csv 取得中...")
            path = download_month(y, m, month_dir)
        lookup.update(load_racelist(path))

    matched = {}
    unmatched = []
    for rid in target_ids:
        if rid in lookup and lookup[rid][0]:
            matched[rid] = lookup[rid]
        else:
            unmatched.append(rid)

    print(f"[突合結果] matched={len(matched)} unmatched={len(unmatched)}")
    if unmatched:
        print(f"  unmatched例: {unmatched[:10]}")

    if not args.apply:
        print("[dry-run] --apply なしのためDB書き込みは行っていません")
        for rid in list(matched)[:5]:
            print(f"  {rid} -> distance={matched[rid][0]} race_name={matched[rid][1]}")
        return

    backup_dir = Path(args.backup_dir) if args.backup_dir else DB_PATH.parent
    backup_path = backup_dir / 'nar_keiba_backup_before_distance_repair.db'
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(backup_path))
    src.backup(dst)
    dst.close()
    src.close()
    print(f"[backup] {backup_path}")

    conn2 = sqlite3.connect(str(DB_PATH))
    conn2.execute("PRAGMA journal_mode=WAL")
    before = conn2.execute("""
        SELECT COUNT(*) FROM nar_races WHERE race_id LIKE '2026%'
          AND (distance IS NULL OR distance=0) AND SUBSTR(race_id,7,2) IN ('06','07','08')
    """).fetchone()[0]

    n_updated = 0
    for rid, (distance, race_name) in matched.items():
        cur = conn2.execute("""
            UPDATE nar_races SET distance=?, race_name=?
            WHERE race_id=? AND (distance IS NULL OR distance=0)
        """, (distance, race_name, rid))
        n_updated += cur.rowcount
    conn2.commit()

    after = conn2.execute("""
        SELECT COUNT(*) FROM nar_races WHERE race_id LIKE '2026%'
          AND (distance IS NULL OR distance=0) AND SUBSTR(race_id,7,2) IN ('06','07','08')
    """).fetchone()[0]
    print(f"[反映結果] UPDATE件数={n_updated}, 欠損 {before} -> {after}")

    check = conn2.execute("PRAGMA integrity_check").fetchone()
    print(f"[integrity_check] {check}")
    conn2.close()


if __name__ == '__main__':
    main()
