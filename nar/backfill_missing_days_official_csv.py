"""backfill_missing_days_official_csv.py - keiba.go.jp公式CSVから
nar_keiba.dbの日単位欠落(既存netkeibaスクレイプが丸ごと抜けている日)を埋める。

対象(2026-08-18時点でreconcile_official_csv.pyにより判明):
  浦和 2026-06-22 / 浦和 2026-06-23 / 浦和 2026-07-16 / 大井 2026-07-24(一部)

安全設計:
  - INSERT OR IGNORE のみ(既存行は一切UPDATE/DELETEしない)
  - 実行前に conn.backup() でバックアップを取る(shutil.copyは使わない)
  - odds列は公式オッズCSV(未取得)がないため NULL のまま(捏造しない)
  - class_code は既存の netkeiba 由来 class_code 体系と混同しないよう空文字のまま
    (fix_nar_class_codes.py の教訓: class_code は grid search なしに変更禁止)

使い方:
  python nar/backfill_missing_days_official_csv.py --csv-root <展開済みCSVの親> --dry-run
  python nar/backfill_missing_days_official_csv.py --csv-root <...> --apply
"""
import argparse
import csv
import sqlite3
from pathlib import Path

PROJ = Path(__file__).parent.parent
DB_PATH = PROJ / 'nar_keiba.db'

VENUE_CODE = {'浦和': '42', '船橋': '43', '大井': '44', '川崎': '45'}

TARGET_DAYS = {
    ('浦和', '20260622'),
    ('浦和', '20260623'),
    ('浦和', '20260716'),
    ('大井', '20260724'),
}

BET_NAMES_SINGLE = {
    'tansho': ('単勝組番', '単勝払戻金（円）', '単勝人気'),
    'wakuren': ('枠複組番1', '枠複組番2', '枠複払戻金（円）', '枠複人気'),
    '枠単': ('枠単組番1', '枠単組番2', '枠単払戻金（円）', '枠単人気'),
    'umaren': ('馬複組番1', '馬複組番2', '馬複払戻金（円）', '馬複人気1'),
    'umatan': ('馬単組番1', '馬単組番2', '馬単払戻金（円）', '馬単人気1'),
}
SANREN = {
    'sanrenpuku': ('３連複組番馬番1', '３連複組番馬番2', '３連複組番馬番3', '３連複払戻金（円）', '３連複人気'),
    'sanrentan': ('３連単組番馬番1', '３連単組番馬番2', '３連単組番馬番3', '３連単払戻金（円）', '３連単人気'),
}


def race_id_from_csv(date8, venue_name, race_num):
    cd = VENUE_CODE.get(venue_name)
    if cd is None:
        return None
    return f"{date8[0:4]}{cd}{date8[4:6]}{date8[6:8]}{int(race_num):02d}"


def parse_time(time_txt):
    """CSVの'タイム'は区切りなし数字(末尾1桁=1/10秒)。例: '1244' -> 1分24秒4"""
    t = (time_txt or '').strip()
    if not t.isdigit():
        return None, None
    decisec = int(t[-1])
    rest = t[:-1]
    if len(rest) <= 2:
        minutes, seconds = 0, int(rest) if rest else 0
    else:
        minutes, seconds = int(rest[:-2]), int(rest[-2:])
    time_sec = minutes * 60 + seconds + decisec / 10
    time_str = f"{minutes}:{seconds:02d}.{decisec}"
    return time_str, time_sec


def parse_bw_diff(txt):
    txt = (txt or '').strip()
    if not txt:
        return None
    try:
        return int(txt)
    except ValueError:
        return None


def to_int(txt):
    txt = (txt or '').strip()
    return int(txt) if txt.isdigit() else None


def to_float(txt):
    txt = (txt or '').strip()
    try:
        return float(txt)
    except ValueError:
        return None


def load_races(path):
    """race_id -> race_dict (nar_races用)。対象日のみ"""
    out = {}
    with open(path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            venue = row['競馬場']
            date8 = row['競走年月日']
            if (venue, date8) not in TARGET_DAYS:
                continue
            rid = race_id_from_csv(date8, venue, row['レース番号'])
            out[rid] = {
                'race_id': rid,
                'date': f"{date8[0:4]}-{date8[4:6]}-{date8[6:8]}",
                'venue_cd': VENUE_CODE[venue],
                'venue_name': venue,
                'race_num': int(row['レース番号']),
                'race_name': row['レース名'].strip(),
                'class_code': '',
                'distance': to_int(row['距離']),
                'track_type': 'ダート',
                'direction': row['回り'].strip() or None,
                'weather': row['天候'].strip() or None,
                'condition': row['馬場'].strip() or None,
                'heads_count': to_int(row['頭数']),
            }
    return out


def load_horses(path):
    """race_id -> [result_dict, ...] (nar_results用)。対象日のみ"""
    out = {}
    with open(path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            venue = row['競馬場']
            date8 = row['競走年月日']
            if (venue, date8) not in TARGET_DAYS:
                continue
            rid = race_id_from_csv(date8, venue, row['レース番号'])
            umaban = to_int(row['馬番'])
            if umaban is None:
                continue
            time_str, time_sec = parse_time(row['タイム'])
            out.setdefault(rid, []).append({
                'race_id': rid,
                'finish': to_int(row['着順']),
                'waku': to_int(row['枠番']),
                'umaban': umaban,
                'horse_name': row['馬名'].strip(),
                'sex': row['性'].strip(),
                'age': to_int(row['齢']),
                'weight_carried': to_float(row['負担重量']),
                'jockey': row['騎手名'].strip(),
                'stable': row['調教師'].strip(),
                'time_str': time_str,
                'time_sec': time_sec,
                'margin': row['着差'].strip(),
                'popularity': to_int(row['人気']),
                'odds': None,
                'last_3f': to_float(row['上がり3F']),
                'body_weight': to_int(row['馬体重']),
                'body_weight_diff': parse_bw_diff(row['馬体重増減']),
            })
    return out


def load_dividends(path):
    """race_id -> [dividend_dict, ...] (nar_dividends用)。対象日のみ"""
    out = {}
    with open(path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            venue = row['競馬場']
            date8 = row['競走年月日']
            if (venue, date8) not in TARGET_DAYS:
                continue
            rid = race_id_from_csv(date8, venue, row['レース番号'])
            divs = out.setdefault(rid, [])

            # 単勝(単一)
            umaban = row['単勝組番'].strip()
            payout = row['単勝払戻金（円）'].strip()
            if umaban.isdigit() and payout.isdigit():
                pop = row['単勝人気'].strip()
                divs.append({'race_id': rid, 'bet_type': 'tansho', 'combination': umaban,
                              'payout': int(payout), 'pop_rank': to_int(pop)})

            # 複勝(最大3件、1着ずつ単一馬番)
            for i in (1, 2, 3):
                c = row[f'複勝組番{i}'].strip()
                p = row[f'複勝払戻金{i}（円）'].strip()
                pr = row[f'複勝人気{i}'].strip()
                if c.isdigit() and p.isdigit():
                    divs.append({'race_id': rid, 'bet_type': 'fukusho', 'combination': c,
                                  'payout': int(p), 'pop_rank': to_int(pr)})

            # 枠複/枠単/馬複/馬単(単一ペア。既存DB慣行に合わせ区切りなし連結)
            for bt, cols in BET_NAMES_SINGLE.items():
                if bt == 'tansho':
                    continue
                c1, c2, p, pr = cols
                v1, v2, pay, pop = row[c1].strip(), row[c2].strip(), row[p].strip(), row[pr].strip()
                if v1.isdigit() and v2.isdigit() and pay.isdigit():
                    divs.append({'race_id': rid, 'bet_type': bt, 'combination': v1 + v2,
                                  'payout': int(pay), 'pop_rank': to_int(pop)})

            # ワイド(最大3組、馬番ペア。今回の修正済みフォーマットに合わせダッシュ区切り)
            for i in (1, 2, 3):
                v1 = row[f'ワイド組番{i}馬番1'].strip()
                v2 = row[f'ワイド組番{i}馬番2'].strip()
                pay = row[f'ワイド払戻金{i}（円）'].strip()
                pop = row[f'ワイド人気{i}'].strip()
                if v1.isdigit() and v2.isdigit() and pay.isdigit():
                    divs.append({'race_id': rid, 'bet_type': 'wide', 'combination': f"{v1}-{v2}",
                                  'payout': int(pay), 'pop_rank': to_int(pop)})

            # 3連複/3連単(単一、区切りなし連結)
            for bt, cols in SANREN.items():
                c1, c2, c3, p, pr = cols
                v1, v2, v3 = row[c1].strip(), row[c2].strip(), row[c3].strip()
                pay, pop = row[p].strip(), row[pr].strip()
                if v1.isdigit() and v2.isdigit() and v3.isdigit() and pay.isdigit():
                    divs.append({'race_id': rid, 'bet_type': bt, 'combination': v1 + v2 + v3,
                                  'payout': int(pay), 'pop_rank': to_int(pop)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv-root', required=True)
    ap.add_argument('--months', nargs='+', required=True, help='例: 2026_6 2026_7')
    ap.add_argument('--apply', action='store_true', help='指定しなければdry-run(件数集計のみ)')
    ap.add_argument('--backup-dir', default=None)
    args = ap.parse_args()

    races, horses, divs = {}, {}, {}
    for ym in args.months:
        y, m = ym.split('_')
        ym_label = f'{y}{int(m):02d}'
        csv_dir = Path(args.csv_root) / f'race_{ym}'
        races.update(load_races(csv_dir / f'{ym_label}_racelist.csv'))
        h = load_horses(csv_dir / f'{ym_label}_horselist.csv')
        for rid, rows in h.items():
            horses.setdefault(rid, []).extend(rows)
        d = load_dividends(csv_dir / f'{ym_label}_payback.csv')
        for rid, rows in d.items():
            divs.setdefault(rid, []).extend(rows)

    n_races = len(races)
    n_horses = sum(len(v) for v in horses.values())
    n_divs = sum(len(v) for v in divs.values())
    print(f"[抽出結果] races={n_races} horse_rows={n_horses} dividend_rows={n_divs}")
    for rid in sorted(races):
        print(f"  {rid} {races[rid]['date']} {races[rid]['venue_name']} R{races[rid]['race_num']} "
              f"{races[rid]['race_name']} 頭数={races[rid]['heads_count']} 出走行={len(horses.get(rid, []))}")

    if not args.apply:
        print("[dry-run] --apply なしのためDB書き込みは行っていません")
        return

    if args.backup_dir:
        backup_path = Path(args.backup_dir) / 'nar_keiba_backup_before_backfill.db'
    else:
        backup_path = DB_PATH.parent / 'nar_keiba_backup_before_backfill.db'
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(backup_path))
    src.backup(dst)
    dst.close()
    src.close()
    print(f"[backup] {backup_path}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    before_races = conn.execute("SELECT COUNT(*) FROM nar_races").fetchone()[0]
    before_results = conn.execute("SELECT COUNT(*) FROM nar_results").fetchone()[0]
    before_divs = conn.execute("SELECT COUNT(*) FROM nar_dividends").fetchone()[0]

    for rid, r in races.items():
        conn.execute("""
            INSERT OR IGNORE INTO nar_races
            (race_id,date,venue_cd,venue_name,race_num,race_name,class_code,
             distance,track_type,direction,weather,condition,heads_count)
            VALUES(:race_id,:date,:venue_cd,:venue_name,:race_num,:race_name,:class_code,
                   :distance,:track_type,:direction,:weather,:condition,:heads_count)
        """, r)
    for rid, rows in horses.items():
        conn.executemany("""
            INSERT OR IGNORE INTO nar_results
            (race_id,finish,waku,umaban,horse_name,sex,age,weight_carried,jockey,stable,
             time_str,time_sec,margin,popularity,odds,last_3f,body_weight,body_weight_diff)
            VALUES(:race_id,:finish,:waku,:umaban,:horse_name,:sex,:age,:weight_carried,
                   :jockey,:stable,:time_str,:time_sec,:margin,:popularity,:odds,
                   :last_3f,:body_weight,:body_weight_diff)
        """, rows)
    for rid, rows in divs.items():
        conn.executemany("""
            INSERT OR IGNORE INTO nar_dividends
            (race_id,bet_type,combination,payout,pop_rank)
            VALUES(:race_id,:bet_type,:combination,:payout,:pop_rank)
        """, rows)
    conn.commit()

    after_races = conn.execute("SELECT COUNT(*) FROM nar_races").fetchone()[0]
    after_results = conn.execute("SELECT COUNT(*) FROM nar_results").fetchone()[0]
    after_divs = conn.execute("SELECT COUNT(*) FROM nar_dividends").fetchone()[0]
    print(f"[反映結果] nar_races {before_races}->{after_races} (+{after_races-before_races})")
    print(f"[反映結果] nar_results {before_results}->{after_results} (+{after_results-before_results})")
    print(f"[反映結果] nar_dividends {before_divs}->{after_divs} (+{after_divs-before_divs})")
    conn.close()


if __name__ == '__main__':
    main()
