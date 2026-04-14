"""レース結果を月間JSONに蓄積するスクリプト

saturday_results_0404.json + weekend_predictions (Saturday) → monthly_results_2026_04.json に追記

Usage:
  python save_results.py saturday_results_0404.json saturday_predictions.json
  python save_results.py sunday_results_0405.json sunday_predictions.json
"""

import json, sys, os, sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = 'keiba.db'


def lookup_dividends(conn, race_id, date, venue, race_num):
    """Return dict of dividend fields for a race, or None if not in DB yet."""
    if conn is None:
        return None
    row = conn.execute(
        "SELECT tansho_payout, umaren_uma1, umaren_uma2, umaren_payout "
        "FROM dividends WHERE race_id=?",
        (race_id,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT tansho_payout, umaren_uma1, umaren_uma2, umaren_payout "
            "FROM dividends WHERE date=? AND venue=? AND race_num=?",
            (date, venue, race_num),
        ).fetchone()
    if row is None:
        return None
    return {
        'tansho_payout': row[0],
        'umaren_uma1': row[1],
        'umaren_uma2': row[2],
        'umaren_payout': row[3],
    }


def calc_v6_return(honmei_odds, h_finish, ni_finish, dividends):
    """v6 normal payout per CLAUDE.md.
    Return (return_yen, source) where source in {'db','approx'}.
    odds>=8: bet 500/1500 (tansho/umaren). odds<8: bet 1000/1000.
    """
    try:
        odds = float(honmei_odds or 0)
    except Exception:
        odds = 0.0
    high = odds >= 8
    tansho_units = 5 if high else 10   # 100yen units
    umaren_units = 15 if high else 10
    ret = 0
    if dividends is not None:
        if h_finish == 1 and dividends['tansho_payout']:
            ret += dividends['tansho_payout'] * tansho_units
        if dividends['umaren_payout'] and h_finish in (1, 2) and ni_finish in (1, 2) and h_finish != ni_finish:
            ret += dividends['umaren_payout'] * umaren_units
        return ret, 'db'
    # Fallback: tansho-only approximation from honmei odds
    if h_finish == 1:
        ret = int(odds * (500 if high else 1000))
    return ret, 'approx'


def main():
    if len(sys.argv) < 3:
        print("Usage: python save_results.py <results.json> <predictions.json> [YYYYMMDD]")
        return

    results_path = sys.argv[1]
    preds_path = sys.argv[2]
    # 3番目の引数で対象日を明示指定可能（指定なしならファイル名 or 今日）
    override_date = sys.argv[3] if len(sys.argv) >= 4 else None

    results = json.load(open(results_path, encoding='utf-8'))
    preds = json.load(open(preds_path, encoding='utf-8'))

    db_conn = None
    if Path(DB_PATH).exists():
        try:
            db_conn = sqlite3.connect(DB_PATH)
        except Exception as e:
            print(f"WARN: failed to open {DB_PATH}: {e}")
            db_conn = None

    # Build prediction lookup
    pred_dict = {}
    for p in preds:
        rid = p['race']['race_id']
        pred_dict[rid] = p

    # 対象日の決定: 引数 → results_YYYYMMDD.jsonから抽出 → datetime.now()
    import re as _re
    if override_date:
        today = datetime.strptime(override_date, '%Y%m%d')
    else:
        m = _re.search(r'results_(\d{8})\.json', str(results_path))
        if m:
            today = datetime.strptime(m.group(1), '%Y%m%d')
        else:
            today = datetime.now()
    month_key = today.strftime('%Y_%m')
    monthly_file = Path(f'monthly_results_{month_key}.json')

    # Load existing monthly data
    if monthly_file.exists():
        monthly = json.load(open(monthly_file, encoding='utf-8'))
    else:
        monthly = {'month': month_key, 'days': []}

    # Check if this day already exists
    existing_dates = {d['date'] for d in monthly['days']}

    day_entry = {
        'date': today.strftime('%Y-%m-%d'),
        'track_conditions': {},
        'buy_results': [],
        'summary': {'races': 0, 'cost': 0, 'return': 0},
    }

    # Track conditions by venue+surface
    for r in results:
        venue = r.get('venue', '')
        cond = r.get('track_cond', '')
        if venue and cond:
            day_entry['track_conditions'][venue] = cond

    # Match buy predictions to results
    for r in results:
        rid = r['race_id']
        p = pred_dict.get(rid)
        if not p:
            continue

        bt = p.get('buy_type', '')
        sp = p.get('special_horse')
        if not bt and not sp:
            continue

        race = p['race']
        honmei = p['honmei']
        ni = p.get('ni', {})
        horses = r.get('horses', [])

        # Find honmei finish
        h_finish = None
        for rh in horses:
            if rh['name'] == honmei['horse_name']:
                h_finish = rh['finish']
                break

        # Find ni finish
        ni_finish = None
        if ni:
            for rh in horses:
                if rh['name'] == ni.get('horse_name', ''):
                    ni_finish = rh['finish']
                    break

        # Calculate P&L
        cost = 0
        ret = 0
        payout_source = None
        winner = horses[0] if horses else {}

        dividends = lookup_dividends(
            db_conn,
            rid,
            today.strftime('%Y-%m-%d'),
            race.get('venue', ''),
            race.get('race_num', 0),
        )

        if bt:
            if bt == 'v6_challenge':
                cost = 1000
                if h_finish == 1:
                    if dividends and dividends['tansho_payout']:
                        ret = dividends['tansho_payout'] * 10
                        payout_source = 'db'
                    else:
                        try:
                            ret = int(float(honmei.get('odds', 0)) * 1000)
                            payout_source = 'approx'
                        except Exception:
                            ret = 0
            else:
                # v6_star2 / v6_star3 = normal buy (tansho + umaren)
                cost = 2000
                ret, payout_source = calc_v6_return(
                    honmei.get('odds', 0), h_finish, ni_finish, dividends
                )

        if sp:
            sp_finish = None
            for rh in horses:
                if rh['name'] == sp['horse_name']:
                    sp_finish = rh['finish']
                    break
            cost += 1000
            if sp_finish == 1:
                if dividends and dividends['tansho_payout']:
                    ret += dividends['tansho_payout'] * 10
                    payout_source = payout_source or 'db'
                else:
                    try:
                        ret += int(float(sp.get('odds', 0)) * 1000)
                        payout_source = payout_source or 'approx'
                    except Exception:
                        pass

        entry = {
            'venue': race.get('venue', ''),
            'race_num': race.get('race_num', 0),
            'race_name': race.get('race_name', ''),
            'grade': p.get('grade', ''),
            'buy_type': bt or ('special' if sp else ''),
            'honmei': honmei['horse_name'],
            'honmei_finish': h_finish,
            'honmei_odds': honmei.get('odds', 0),
            'ni': ni.get('horse_name', '') if ni else '',
            'ni_finish': ni_finish,
            'winner': winner.get('name', ''),
            'winner_odds': winner.get('odds', ''),
            'track_cond': r.get('track_cond', ''),
            'cost': cost,
            'return': ret,
            'profit': ret - cost,
            'payout_source': payout_source or 'none',
        }
        if sp:
            entry['special'] = sp['horse_name']
            entry['special_rule'] = sp.get('rule', '')

        day_entry['buy_results'].append(entry)
        day_entry['summary']['races'] += 1
        day_entry['summary']['cost'] += cost
        day_entry['summary']['return'] += ret

    day_entry['summary']['profit'] = day_entry['summary']['return'] - day_entry['summary']['cost']
    day_entry['summary']['roi'] = round(day_entry['summary']['return'] / day_entry['summary']['cost'] * 100, 1) if day_entry['summary']['cost'] > 0 else 0

    # Add or update
    if day_entry['date'] in existing_dates:
        monthly['days'] = [d for d in monthly['days'] if d['date'] != day_entry['date']]
    monthly['days'].append(day_entry)
    monthly['days'].sort(key=lambda d: d['date'])

    # Recalculate monthly totals
    m_cost = sum(d['summary']['cost'] for d in monthly['days'])
    m_ret = sum(d['summary']['return'] for d in monthly['days'])
    monthly['total'] = {
        'days': len(monthly['days']),
        'races': sum(d['summary']['races'] for d in monthly['days']),
        'cost': m_cost,
        'return': m_ret,
        'profit': m_ret - m_cost,
        'roi': round(m_ret / m_cost * 100, 1) if m_cost > 0 else 0,
    }

    with open(monthly_file, 'w', encoding='utf-8') as f:
        json.dump(monthly, f, ensure_ascii=False, indent=2)

    print(f"Saved to {monthly_file}")
    print(f"  Today: {day_entry['summary']['races']}R, cost={day_entry['summary']['cost']:,}, ret={day_entry['summary']['return']:,}, P&L={day_entry['summary']['profit']:+,}")
    print(f"  Month: {monthly['total']['races']}R, ROI={monthly['total']['roi']}%, P&L={monthly['total']['profit']:+,}")


if __name__ == '__main__':
    main()
