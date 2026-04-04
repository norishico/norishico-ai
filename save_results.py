"""レース結果を月間JSONに蓄積するスクリプト

saturday_results_0404.json + weekend_predictions (Saturday) → monthly_results_2026_04.json に追記

Usage:
  python save_results.py saturday_results_0404.json saturday_predictions.json
  python save_results.py sunday_results_0405.json sunday_predictions.json
"""

import json, sys, os
from pathlib import Path
from datetime import datetime

def main():
    if len(sys.argv) < 3:
        print("Usage: python save_results.py <results.json> <predictions.json>")
        return

    results_path = sys.argv[1]
    preds_path = sys.argv[2]

    results = json.load(open(results_path, encoding='utf-8'))
    preds = json.load(open(preds_path, encoding='utf-8'))

    # Build prediction lookup
    pred_dict = {}
    for p in preds:
        rid = p['race']['race_id']
        pred_dict[rid] = p

    # Determine date from results
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
        winner = horses[0] if horses else {}

        if bt:
            if bt == 'v6_challenge':
                cost = 1000
                if h_finish == 1:
                    try:
                        ret = int(float(honmei.get('odds', 0)) * 1000)
                    except:
                        ret = 0
            else:
                cost = 2000
                if h_finish == 1:
                    try:
                        ret += int(float(honmei.get('odds', 0)) * 1000)
                    except:
                        pass
                # umaren check would need dividend data - approximate
                # For now just track tansho

        if sp:
            sp_finish = None
            for rh in horses:
                if rh['name'] == sp['horse_name']:
                    sp_finish = rh['finish']
                    break
            cost += 1000
            if sp_finish == 1:
                try:
                    ret += int(float(sp.get('odds', 0)) * 1000)
                except:
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
