"""reconcile_official_csv.py - keiba.go.jp公式CSV(南関東)とnar_keiba.dbの突合検査

Step 1 (計画: project_nar_official_csv候補): 公式CSVが既存DBの正データとして
信頼できるかを、着順・人気・単勝払戻の一致率で検証する。read-only。

使い方:
  python nar/reconcile_official_csv.py --csv-dir <公式CSV展開済みディレクトリの親>
    (races_YYYY_M/YYYYMM_racelist.csv 等が入っているディレクトリを想定)
"""
import argparse
import csv
import sqlite3
from pathlib import Path

PROJ = Path(__file__).parent.parent
DB_PATH = PROJ / 'nar_keiba.db'

VENUE_CODE = {'浦和': '42', '船橋': '43', '大井': '44', '川崎': '45'}


def race_id_from_csv(date8, venue_name, race_num):
    cd = VENUE_CODE.get(venue_name)
    if cd is None:
        return None
    return f"{date8[0:4]}{cd}{date8[4:6]}{date8[6:8]}{int(race_num):02d}"


def load_horselist(path):
    """race_id -> {umaban: {finish, popularity}}"""
    out = {}
    with open(path, encoding='utf-8-sig') as f:
        r = csv.DictReader(f)
        for row in r:
            venue = row['競馬場']
            if venue not in VENUE_CODE:
                continue
            rid = race_id_from_csv(row['競走年月日'], venue, row['レース番号'])
            umaban = row['馬番']
            if not umaban:
                continue
            finish = row['着順'].strip()
            pop = row['人気'].strip()
            out.setdefault(rid, {})[int(umaban)] = {
                'finish': int(finish) if finish.isdigit() else None,
                'popularity': int(pop) if pop.isdigit() else None,
            }
    return out


def load_payback_tansho(path):
    """race_id -> (win_umaban, win_payout) or None if no data"""
    out = {}
    with open(path, encoding='utf-8-sig') as f:
        r = csv.DictReader(f)
        for row in r:
            venue = row['競馬場']
            if venue not in VENUE_CODE:
                continue
            rid = race_id_from_csv(row['競走年月日'], venue, row['レース番号'])
            umaban = row['単勝組番'].strip()
            payout = row['単勝払戻金（円）'].strip()
            if umaban.isdigit() and payout.isdigit():
                out[rid] = (int(umaban), int(payout))
    return out


def load_racelist_keys(path):
    """race_idの集合(レース単位の突合率用)"""
    out = set()
    with open(path, encoding='utf-8-sig') as f:
        r = csv.DictReader(f)
        for row in r:
            venue = row['競馬場']
            if venue not in VENUE_CODE:
                continue
            rid = race_id_from_csv(row['競走年月日'], venue, row['レース番号'])
            if rid:
                out.add(rid)
    return out


def check_month(conn, csv_dir, ym_label):
    race_csv = csv_dir / f'{ym_label}_racelist.csv'
    horse_csv = csv_dir / f'{ym_label}_horselist.csv'
    pay_csv = csv_dir / f'{ym_label}_payback.csv'

    csv_race_ids = load_racelist_keys(race_csv)
    csv_horses = load_horselist(horse_csv)
    csv_tansho = load_payback_tansho(pay_csv)

    n_csv_races = len(csv_race_ids)

    db_race_ids = set()
    cur = conn.execute(
        "SELECT DISTINCT race_id FROM nar_races WHERE race_id IN ({})".format(
            ','.join('?' * len(csv_race_ids))
        ),
        list(csv_race_ids),
    ) if csv_race_ids else None
    if cur:
        db_race_ids = {row[0] for row in cur.fetchall()}

    matched_races = csv_race_ids & db_race_ids

    # 馬単位: finish/popularity一致率
    n_horse_pairs = 0
    n_finish_match = 0
    n_pop_match = 0
    finish_mismatches = []
    pop_mismatches = []

    for rid in matched_races:
        cur = conn.execute(
            "SELECT umaban, finish, popularity FROM nar_results WHERE race_id=?", (rid,)
        )
        db_horses = {row[0]: {'finish': row[1], 'popularity': row[2]} for row in cur.fetchall()}
        csv_h = csv_horses.get(rid, {})
        for umaban, cvals in csv_h.items():
            dvals = db_horses.get(umaban)
            if dvals is None:
                continue
            n_horse_pairs += 1
            if cvals['finish'] is not None and dvals['finish'] is not None:
                if cvals['finish'] == dvals['finish']:
                    n_finish_match += 1
                elif len(finish_mismatches) < 5:
                    finish_mismatches.append((rid, umaban, dvals['finish'], cvals['finish']))
            if cvals['popularity'] is not None and dvals['popularity'] is not None:
                if cvals['popularity'] == dvals['popularity']:
                    n_pop_match += 1
                elif len(pop_mismatches) < 5:
                    pop_mismatches.append((rid, umaban, dvals['popularity'], cvals['popularity']))

    # 単勝払戻一致率: DB nar_dividendsにcombination==umaban and payout==payoutの行があるか
    n_tansho_checked = 0
    n_tansho_match = 0
    tansho_mismatches = []
    for rid in matched_races:
        csv_win = csv_tansho.get(rid)
        if csv_win is None:
            continue
        n_tansho_checked += 1
        win_umaban, win_payout = csv_win
        cur = conn.execute(
            "SELECT 1 FROM nar_dividends WHERE race_id=? AND TRIM(combination)=? AND payout=? LIMIT 1",
            (rid, str(win_umaban), win_payout),
        )
        if cur.fetchone():
            n_tansho_match += 1
        elif len(tansho_mismatches) < 5:
            cur2 = conn.execute(
                "SELECT bet_type, combination, payout FROM nar_dividends WHERE race_id=?", (rid,)
            )
            tansho_mismatches.append((rid, win_umaban, win_payout, cur2.fetchall()))

    return {
        'ym': ym_label,
        'n_csv_races': n_csv_races,
        'n_matched_races': len(matched_races),
        'n_horse_pairs': n_horse_pairs,
        'n_finish_match': n_finish_match,
        'n_pop_match': n_pop_match,
        'n_tansho_checked': n_tansho_checked,
        'n_tansho_match': n_tansho_match,
        'finish_mismatches': finish_mismatches,
        'pop_mismatches': pop_mismatches,
        'tansho_mismatches': tansho_mismatches,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv-root', required=True, help='race_YYYY_M フォルダ群の親ディレクトリ')
    ap.add_argument('--months', nargs='+', required=True, help='例: 2026_5 2026_6 2026_7')
    ap.add_argument('--out', default=None, help='結果を書き出すテキストファイル')
    args = ap.parse_args()

    conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    conn.execute('PRAGMA query_only=1')

    lines = []
    for ym in args.months:
        y, m = ym.split('_')
        ym_label = f'{y}{int(m):02d}'
        csv_dir = Path(args.csv_root) / f'race_{ym}'
        res = check_month(conn, csv_dir, ym_label)

        race_rate = res['n_matched_races'] / res['n_csv_races'] * 100 if res['n_csv_races'] else 0
        finish_rate = res['n_finish_match'] / res['n_horse_pairs'] * 100 if res['n_horse_pairs'] else 0
        pop_rate = res['n_pop_match'] / res['n_horse_pairs'] * 100 if res['n_horse_pairs'] else 0
        tansho_rate = res['n_tansho_match'] / res['n_tansho_checked'] * 100 if res['n_tansho_checked'] else 0

        lines.append(f"=== {ym_label} ===")
        lines.append(
            f"レース突合: {res['n_matched_races']}/{res['n_csv_races']} ({race_rate:.1f}%)"
        )
        lines.append(
            f"着順一致: {res['n_finish_match']}/{res['n_horse_pairs']} ({finish_rate:.1f}%)"
        )
        lines.append(
            f"人気一致: {res['n_pop_match']}/{res['n_horse_pairs']} ({pop_rate:.1f}%)"
        )
        lines.append(
            f"単勝払戻一致: {res['n_tansho_match']}/{res['n_tansho_checked']} ({tansho_rate:.1f}%)"
        )
        if res['finish_mismatches']:
            lines.append(f"着順不一致例: {res['finish_mismatches']}")
        if res['pop_mismatches']:
            lines.append(f"人気不一致例: {res['pop_mismatches']}")
        if res['tansho_mismatches']:
            lines.append(f"単勝払戻不一致例: {res['tansho_mismatches']}")
        lines.append("")

    text = "\n".join(lines)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(text)
    else:
        with open('reconcile_result_fallback.txt', 'w', encoding='utf-8') as f:
            f.write(text)

    conn.close()


if __name__ == '__main__':
    main()
