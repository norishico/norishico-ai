"""レース結果を月間JSONに蓄積するスクリプト

saturday_results_0404.json + weekend_predictions (Saturday) → monthly_results_2026_04.json に追記

Usage:
  python save_results.py saturday_results_0404.json saturday_predictions.json
  python save_results.py sunday_results_0405.json sunday_predictions.json
  python save_results.py --reclassify              # 既存monthly_resultsの外れ分類を再計算
"""

import json, sys, os, sqlite3
from pathlib import Path
from datetime import datetime
from collections import Counter

DB_PATH = 'keiba.db'


# ---------------------------------------------------------------------------
# 外れの4タイプ分類 (凱旋門太郎フレームワーク)
# ---------------------------------------------------------------------------
# luck     : 運のぶれ (本命1-3着、惜敗)     → 行動維持が正解
# narrow   : 読み甘   (本命4-5着)           → 見立て微調整
# misread  : 読み違い (本命6着+)            → モデル要振り返り
# scenario : 展開依存 (同日同会場3敗+で4着+) → 条件読み一括ミス
# ---------------------------------------------------------------------------

def classify_misses(buy_results):
    """buy_results リストに miss_type を付与 (in-place)。

    的中(profit>0)は miss_type=None。
    外れは着順ベースで分類し、同日同会場に外れが3件以上集中して
    本命4着以下なら 'scenario' (展開依存) で上書きする。
    """
    # Phase 1: 着順ベース分類
    for entry in buy_results:
        if entry.get('profit', 0) > 0:
            entry['miss_type'] = None
            continue
        finish = entry.get('honmei_finish')
        if finish is None:
            entry['miss_type'] = 'unknown'
        elif finish <= 3:
            entry['miss_type'] = 'luck'
        elif finish <= 5:
            entry['miss_type'] = 'narrow'
        else:
            entry['miss_type'] = 'misread'

    # Phase 2: 展開依存の検出 (同日同会場で3敗以上)
    venue_losses = Counter()
    for entry in buy_results:
        if entry.get('profit', 0) <= 0:
            finish = entry.get('honmei_finish') or 99
            if finish >= 4:
                venue_losses[entry.get('venue', '')] += 1

    scenario_venues = {v for v, cnt in venue_losses.items() if cnt >= 3}

    for entry in buy_results:
        if (entry.get('venue', '') in scenario_venues
                and entry.get('profit', 0) <= 0
                and (entry.get('honmei_finish') or 99) >= 4):
            entry['miss_type'] = 'scenario'


def update_committee_competition(results_list, today, db_conn):
    """委員会対決の結果を自動更新する。save_results.py main()から呼び出す。"""
    comp_path = Path('dashboard_config/committee_competition.json')
    if not comp_path.exists():
        return

    comp = json.load(open(comp_path, encoding='utf-8'))
    today_str = today.strftime('%Y-%m-%d')

    # (venue, race_num) → {race_id, horses: {name: finish}, horses_list}
    race_map = {}
    for r in results_list:
        venue = r.get('venue', '')
        rnum = int(r.get('race_num', 0))
        horses_raw = r.get('horses', [])
        horses = {h['name'].strip(): h.get('finish') for h in horses_raw}
        race_map[(venue, rnum)] = {
            'race_id': r.get('race_id', ''),
            'horses': horses,
            'horses_list': horses_raw,
        }

    updated = 0
    for entry in comp.get('entries', []):
        if entry.get('date') != today_str:
            continue
        if entry.get('result_ret') is not None:
            continue  # 既に更新済み

        venue = entry.get('venue', '')
        race_num = int(entry.get('race_num', 0))
        pick = (entry.get('pick') or '').strip()
        rdata = race_map.get((venue, race_num))
        if not rdata:
            continue

        # 1着馬名を記録
        for h in rdata['horses_list']:
            if h.get('finish') == 1:
                entry['winner'] = h.get('name', '').strip()
                break

        # 結果データから実際のオッズを取得（宣言時プレースホルダー対策）
        actual_odds = None
        for h in rdata['horses_list']:
            if h.get('name', '').strip() == pick:
                try:
                    actual_odds = float(h.get('odds') or 0) or None
                except (TypeError, ValueError):
                    pass
                break
        if actual_odds:
            entry['pick_odds'] = actual_odds  # 実際のオッズで上書き

        finish = rdata['horses'].get(pick)
        entry['pick_finish'] = finish  # 何着かを記録
        bet = int(entry.get('bet', 2000))
        if finish == 1:
            tansho = None
            if db_conn:
                row = db_conn.execute(
                    "SELECT tansho_payout FROM dividends WHERE race_id=?",
                    (rdata['race_id'],)
                ).fetchone()
                if row:
                    tansho = row[0]
            if tansho:
                entry['result_ret'] = tansho * (bet // 100)
            else:
                try:
                    # actual_oddsを優先、なければpick_odds(ただし≥90はプレースホルダーとして除外)
                    odds_src = actual_odds
                    if not odds_src:
                        raw = float(entry.get('pick_odds') or 0)
                        odds_src = raw if raw < 90 else None
                    if odds_src:
                        entry['result_ret'] = int(odds_src * bet)
                    else:
                        entry['result_ret'] = 0
                        entry['result_ret_note'] = 'odds_unknown_recheck'
                        print(f"  ⚠️ 委員会対決: {entry.get('member')} {venue}{race_num}R pick_odds不明→要再確認")
                except Exception:
                    entry['result_ret'] = 0
        else:
            entry['result_ret'] = 0
        updated += 1

    if updated:
        comp_path.write_text(
            json.dumps(comp, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        print(f"  委員会対決: {updated}件更新 → {comp_path}")


def reclassify_all():
    """既存の monthly_results_*.json 全てに外れ分類を再計算して上書き。"""
    import glob as _glob
    files = sorted(_glob.glob('monthly_results_*.json'))
    if not files:
        print('No monthly_results_*.json found.')
        return
    for fp in files:
        data = json.load(open(fp, encoding='utf-8'))
        changed = 0
        for day in data.get('days', []):
            buys = day.get('buy_results', [])
            if buys:
                classify_misses(buys)
                changed += len(buys)
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f'  {fp}: {changed} entries reclassified')
    print('Done.')


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

    # 外れ分類
    classify_misses(day_entry['buy_results'])

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

    # 委員会対決: 今日の宣言エントリに結果を反映
    update_committee_competition(results, today, db_conn)

    print(f"Saved to {monthly_file}")
    print(f"  Today: {day_entry['summary']['races']}R, cost={day_entry['summary']['cost']:,}, ret={day_entry['summary']['return']:,}, P&L={day_entry['summary']['profit']:+,}")
    print(f"  Month: {monthly['total']['races']}R, ROI={monthly['total']['roi']}%, P&L={monthly['total']['profit']:+,}")


if __name__ == '__main__':
    if '--reclassify' in sys.argv:
        reclassify_all()
    else:
        main()
