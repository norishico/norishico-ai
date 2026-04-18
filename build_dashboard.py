"""NORISHICO KEIBA AI ダッシュボード本番ビルド

dashboard_template.html + 各種データソースを集計して dashboard.html を生成する。

使用法:
  py -X utf8 build_dashboard.py

生成物:
  dashboard.html (プロジェクト直下、単一HTML完結)

データソース:
  - keiba.db (results/dividends/venue_sire_bonus)
  - monthly_results_YYYY_MM.json (実運用実績)
  - dashboard_config/committee.json (メンバー+投稿)
  - dashboard_config/kanban.json (改善管理)
  - dashboard_config/rule_roi.json (バージョン別ROI年表)

BT値(2020-2025)は CLAUDE.md 記載の v6.6 Walk-Forward CV 値をハードコード。
本格運用時は backtest_v6.py --year YYYY の結果ファイルから読むように差し替え可。
"""
from __future__ import annotations
import json, os, sqlite3, sys, io, glob, re
from pathlib import Path
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
PROJ = Path(__file__).parent
DB = PROJ / 'keiba.db'
CFG = PROJ / 'dashboard_config'
TEMPLATE = PROJ / 'dashboard_template.html'
OUTPUT = PROJ / 'dashboard.html'


# ============ 1. BT値(ハードコード、v6.6 確定値) =====================
BT_YEARLY = [
    {'year':2020, 'roi_normal':167.2, 'roi_wins':142.1, 'pl':61400, 'n':84},
    {'year':2021, 'roi_normal':135.9, 'roi_wins':122.3, 'pl':34400, 'n':87},
    {'year':2022, 'roi_normal':174.1, 'roi_wins':148.7, 'pl':214450, 'n':255},
    {'year':2023, 'roi_normal':116.1, 'roi_wins':135.4, 'pl':47850, 'n':272},
    {'year':2024, 'roi_normal':98.4, 'roi_wins':96.2, 'pl':-39650, 'n':289},
    {'year':2025, 'roi_normal':121.3, 'roi_wins':114.5, 'pl':63200, 'n':415},
]


# ============ 2. 実運用2026集計 =======================================
def aggregate_live_2026():
    """monthly_results_2026_*.json から2026年の実値を集計"""
    total_cost, total_return, total_n = 0, 0, 0
    monthly = []
    all_buys = []
    files = sorted(glob.glob(str(PROJ / 'monthly_results_2026_*.json')))
    for f in files:
        try:
            d = json.load(open(f, encoding='utf-8'))
        except Exception as e:
            print(f'  [warn] {f}: {e}')
            continue
        tot = d.get('total', {})
        m = d.get('month','')  # '2026-04'
        mo = int(m.split('-')[1]) if '-' in m else 0
        monthly.append({
            'month':mo,
            'roi': tot.get('roi', 0),
            'cost': tot.get('cost', 0),
            'return': tot.get('return', 0),
            'n': tot.get('races', 0),
        })
        total_cost += tot.get('cost', 0)
        total_return += tot.get('return', 0)
        total_n += tot.get('races', 0)
        # 各レースを buys に
        for day in d.get('days', []):
            date = day.get('date','')
            for br in day.get('buy_results', []):
                br_copy = dict(br)
                br_copy['date'] = date
                all_buys.append(br_copy)

    roi = (total_return / total_cost * 100) if total_cost else 0
    pl = total_return - total_cost
    hits = sum(1 for b in all_buys if b.get('return', 0) > 0)
    hit_rate = (hits / total_n * 100) if total_n else 0
    return {
        'total':{'n':total_n, 'cost':total_cost, 'return':total_return, 'pl':pl, 'roi':round(roi,1), 'hit':hits, 'hit_rate':round(hit_rate,1)},
        'monthly':monthly,
        'buys':all_buys,
    }


# ============ 3. yearly構築(BT + Live) ================================
def build_yearly(live):
    yearly = list(BT_YEARLY)
    yearly.append({'year':2026, 'roi_normal': live['total']['roi'], 'roi_wins': live['total']['roi'], 'pl': live['total']['pl'], 'n': live['total']['n']})
    return yearly


# ============ 4. monthly_roi ヒートマップ ==============================
def build_monthly_roi(live):
    """2021-2026の月別ROI(BTは大まかダミー、2026は実値)"""
    # BT年は固定ダミー(後日実値取得に差替可能)
    rows = [
        {'year':2021, 'months':[None,None,None,112,145,98,132,87,118,102,125,141]},
        {'year':2022, 'months':[None,None,None,162,174,188,195,142,167,178,155,182]},
        {'year':2023, 'months':[None,None,None,108,98,135,158,142,112,95,108,128]},
        {'year':2024, 'months':[None,None,None,85,92,105,98,78,112,95,108,115]},
        {'year':2025, 'months':[None,None,None,118,132,115,128,108,125,118,132,141]},
    ]
    # 2026 実値
    months26 = [None]*12
    for m in live['monthly']:
        idx = m['month'] - 1
        if 0 <= idx < 12:
            months26[idx] = round(m['roi'], 0)
    rows.append({'year':2026, 'months':months26})
    return rows


# ============ 5. reviews(2026実レース) ================================
def build_reviews(live):
    """実運用の全買いレースを振り返り用テーブルに"""
    reviews = []
    for b in live['buys']:
        reviews.append({
            'date': b.get('date',''),
            'race': f"{b.get('venue','')}{b.get('race_num','')}R",
            'rname': b.get('race_name',''),
            'honmei': b.get('honmei',''),
            'odds': b.get('honmei_odds', 0) or 0,
            'buy': _buy_label(b),
            'cost': b.get('cost', 0),
            'finish': (f"{b.get('honmei_finish','')}着" if b.get('honmei_finish') else '-'),
            'ret': b.get('return', 0),
            'miss': b.get('miss_type'),
            'note': _build_note(b),
        })
    # 新しい順
    reviews.sort(key=lambda x: x['date'], reverse=True)
    return reviews


def _buy_label(b):
    bt = b.get('buy_type', '') or ''
    cost = b.get('cost', 0)
    if bt == 'v6_challenge' or cost == 1000:
        return '単勝1,000円'
    if cost == 2000:
        return '単勝500+馬連1,500'
    return f'{cost}円'


def _build_note(b):
    """外れタイプに応じたデフォルトコメント"""
    miss = b.get('miss_type')
    win = b.get('winner','')
    w_odds = b.get('winner_odds','')
    if not miss:
        return '的中' if (b.get('return',0) > 0) else ''
    notes = {
        'luck':'運のぶれ(本命1-3着)。行動維持',
        'narrow':'読み甘(本命4-5着)。微調整でEV改善余地',
        'misread':'読み違い(本命6着+)。要振り返り',
        'scenario':'展開依存外れ(同会場集中/馬場バイアス)',
    }
    base = notes.get(miss, '')
    return f'{base} / 勝ち馬: {win}({w_odds})' if win else base


def build_miss_stats(reviews):
    miss_types = {}
    venue_misses = {v:0 for v in ['札幌','函館','福島','新潟','東京','中山','中京','京都','阪神','小倉']}
    for r in reviews:
        m = r.get('miss')
        if m:
            miss_types[m] = miss_types.get(m, 0) + 1
            venue = r['race'].split('R')[0].rstrip('0123456789')
            if venue in venue_misses:
                venue_misses[venue] += 1
    return miss_types, venue_misses


# ============ 6. SQL集計: オッズ帯別ROI ===============================
def build_odds_roi(conn):
    """results + dividends から オッズ帯別 単勝回収率(%)集計(2020-2025)
    ROI = sum(payout for finish=1) / (全馬数 * 100) * 100
    """
    try:
        # 全馬行を取得(finish=1 か否か問わず)
        rows = conn.execute("""
            SELECT r.odds, r.finish, d.tansho_payout
            FROM results r LEFT JOIN dividends d ON r.race_id = d.race_id
            WHERE r.date >= '2020-01-01' AND r.date < '2026-01-01'
              AND r.odds IS NOT NULL AND r.odds >= 3 AND r.odds < 33
        """).fetchall()
    except Exception as e:
        print(f'  [warn] odds_roi SQL: {e}')
        return []
    bands = [(3,5),(5,7),(7,10),(10,13),(13,16),(16,20),(20,25),(25,33)]
    out = []
    for lo, hi in bands:
        matched = [(o,f,p) for o,f,p in rows if lo <= o < hi]
        n = len(matched)  # この帯の全馬数(各馬1回ベット想定)
        if n == 0:
            out.append({'band':f'{lo}-{hi}倍','roi':0,'n':0})
            continue
        total_payout = sum((p or 0) for _,f,p in matched if f == 1)
        roi = total_payout / (n * 100) * 100  # n票×100円の投資に対する回収率
        out.append({'band':f'{lo}-{hi}倍','roi':round(roi,1),'n':n})
    return out


# ============ 7. 血統ROIランキング TOP10 ==============================
def build_blood_rank(conn, top_n=10, min_n=100):
    """芝良限定で父ごとの単勝回収率集計、全馬数≥min_n
    ROI = sum(payout for finish=1) / (全馬数 * 100) * 100
    """
    try:
        rows = conn.execute("""
            SELECT r.sire, r.finish, d.tansho_payout
            FROM results r LEFT JOIN dividends d ON r.race_id = d.race_id
            WHERE r.date >= '2020-01-01' AND r.date < '2026-01-01'
              AND r.surface = '芝' AND r.track_cond = '良'
              AND r.sire IS NOT NULL AND r.sire != ''
              AND r.odds IS NOT NULL AND r.odds > 0
        """).fetchall()
    except Exception as e:
        print(f'  [warn] blood_rank: {e}')
        return []
    stats = {}
    for sire, finish, payout in rows:
        s = (sire or '').strip()
        if not s: continue
        stats.setdefault(s, {'n':0, 'total':0})
        stats[s]['n'] += 1
        if finish == 1:
            stats[s]['total'] += payout or 0
    ranked = []
    for sire, s in stats.items():
        if s['n'] < min_n: continue
        roi = s['total'] / (s['n'] * 100) * 100
        ranked.append({'name':sire, 'roi':round(roi,0), 'n':s['n']})
    ranked.sort(key=lambda x: -x['roi'])
    return ranked[:top_n]


# ============ 8. 新興種牡馬 推移 ======================================
def build_new_sire(conn):
    """指定産駒の年別単勝回収率(%)推移"""
    sires = ['エピファネイア','モーリス','キズナ','ドレフォン']
    years = [2022, 2023, 2024, 2025, 2026]
    result = {'years':years, 'sires':[]}
    for sire in sires:
        vals = []
        for y in years:
            try:
                rows = conn.execute("""
                    SELECT r.finish, d.tansho_payout
                    FROM results r LEFT JOIN dividends d ON r.race_id = d.race_id
                    WHERE SUBSTR(r.date,1,4) = ? AND r.sire = ?
                      AND r.odds IS NOT NULL AND r.odds > 0
                """, (str(y), sire)).fetchall()
            except Exception:
                vals.append(None); continue
            n = len(rows)
            if n < 20:
                vals.append(None); continue
            total = sum((p or 0) for f, p in rows if f == 1)
            roi = total / (n * 100) * 100
            vals.append(round(roi, 0))
        result['sires'].append({'name':sire, 'vals':vals})
    return result


# ============ 9. 脚質×会場 ROI ========================================
def _style_code(pos4):
    """4角位置から脚質コード"""
    if pos4 is None or pos4 == 0: return None
    if pos4 <= 2: return 0  # 逃げ
    if pos4 <= 5: return 1  # 先行
    if pos4 <= 9: return 2  # 中団
    if pos4 <= 14: return 3 # 差し
    return 4  # 追込


def build_style_venue(conn):
    venues = ['札幌','函館','福島','新潟','東京','中山','中京','京都','阪神','小倉']
    styles = ['逃げ','先行','中団','差し','追込']
    try:
        rows = conn.execute("""
            SELECT r.venue, r.pos4, r.finish, d.tansho_payout
            FROM results r LEFT JOIN dividends d ON r.race_id = d.race_id
            WHERE r.date >= '2020-01-01' AND r.date < '2026-01-01'
              AND r.odds IS NOT NULL AND r.odds > 0
        """).fetchall()
    except Exception as e:
        print(f'  [warn] style_venue: {e}')
        return None
    stats = {}
    for v, pos4, fin, payout in rows:
        sc = _style_code(pos4)
        if v not in venues or sc is None: continue
        key = (v, sc)
        stats.setdefault(key, {'n':0, 'total':0})
        stats[key]['n'] += 1
        if fin == 1:
            stats[key]['total'] += payout or 0
    data = []
    for v in venues:
        row = []
        for sc in range(5):
            s = stats.get((v, sc), {'n':0,'total':0})
            roi = (s['total'] / (s['n']*100) * 100) if s['n'] else 0
            row.append(round(roi, 0))
        data.append(row)
    return {'venues':venues, 'styles':styles, 'data':data}


# ============ 10. 騎手×会場 ROI =======================================
def build_jockey_venue(conn):
    jockeys = ['ルメール','川田','戸崎','武豊','岩田康誠']
    venues = ['東京','中山','阪神','京都','福島']
    try:
        rows = conn.execute("""
            SELECT r.jockey, r.venue, r.finish, d.tansho_payout
            FROM results r LEFT JOIN dividends d ON r.race_id = d.race_id
            WHERE r.date >= '2020-01-01' AND r.date < '2026-01-01'
              AND r.odds IS NOT NULL AND r.odds > 0
        """).fetchall()
    except Exception as e:
        print(f'  [warn] jockey_venue: {e}')
        return None
    stats = {}
    for j, v, fin, payout in rows:
        jn = (j or '').strip()
        if not jn or v not in venues: continue
        matched = None
        for jk in jockeys:
            if jk in jn:
                matched = jk; break
        if not matched: continue
        key = (matched, v)
        stats.setdefault(key, {'n':0, 'total':0})
        stats[key]['n'] += 1
        if fin == 1:
            stats[key]['total'] += payout or 0
    data = []
    for jk in jockeys:
        row = []
        for v in venues:
            s = stats.get((jk, v), {'n':0,'total':0})
            roi = (s['total'] / (s['n']*100) * 100) if s['n'] else 0
            row.append(round(roi, 0))
        data.append(row)
    return {'venues':venues, 'jockeys':jockeys, 'data':data}


# ============ 11. 厩舎×馬場状態 =======================================
def build_stable_track(conn):
    # DBのtrack_condは '良'/'稍'/'重'/'不' の1文字保存
    tracks_short = ['良','稍','重','不']
    tracks = ['良','稍重','重','不良']  # 表示用
    try:
        rows = conn.execute("""
            SELECT r.stable_loc, r.track_cond, r.finish, d.tansho_payout
            FROM results r LEFT JOIN dividends d ON r.race_id = d.race_id
            WHERE r.date >= '2020-01-01' AND r.date < '2026-01-01'
              AND r.odds IS NOT NULL AND r.odds > 0
              AND r.track_cond IN ('良','稍','重','不')
        """).fetchall()
    except Exception as e:
        print(f'  [warn] stable_track: {e}')
        return None
    stats = {}
    for loc, cond, fin, payout in rows:
        # 実DBでは '美' / '栗' で保存されている
        if loc not in ('美','栗'): continue
        loc_full = '美浦' if loc == '美' else '栗東'
        key = (f"{loc_full}(全体)", cond)
        stats.setdefault(key, {'n':0, 'total':0})
        stats[key]['n'] += 1
        if fin == 1:
            stats[key]['total'] += payout or 0
    stables_simple = ['美浦(全体)','栗東(全体)']
    data = []
    for st in stables_simple:
        row = []
        for c_short in tracks_short:
            s = stats.get((st, c_short), {'n':0,'total':0})
            roi = (s['total'] / (s['n']*100) * 100) if s['n'] else 0
            row.append(round(roi, 0))
        data.append(row)
    return {'tracks':tracks, 'stables':stables_simple, 'data':data}


# ============ 12. EV分布/的中率vsROI(ダミー、BT値) ====================
EV_HIST = [
    {'ev':'2-4','n':108},{'ev':'4-6','n':225},{'ev':'6-8','n':312},
    {'ev':'8-10','n':268},{'ev':'10-12','n':198},{'ev':'12-14','n':142},
    {'ev':'14-16','n':89},{'ev':'16-18','n':48},{'ev':'18-20','n':29},
]
HIT_ROI = [
    {'cls':'新馬C2', 'hit':18.2, 'roi':135.4, 'n':48},
    {'cls':'未勝利F1', 'hit':21.5, 'roi':175.2, 'n':102},
    {'cls':'3勝(normal)', 'hit':15.8, 'roi':127.0, 'n':193},
    {'cls':'3勝(challenge)', 'hit':7.2, 'roi':97.3, 'n':56},
    {'cls':'G1/G2', 'hit':12.4, 'roi':112.8, 'n':148},
]


# ============ 13. 取りこぼしログ(見送り好成績) =========================
def build_missed(conn):
    """見送りデータは別途トラッキング機構が必要。最初は実運用ログから推定。"""
    # Phase 1 では空/ダミー
    return [
        {'date':'2026-04-12','race':'阪神11R','horse':'エネルジコ','finish':'1着','odds':'54.9倍','reason':'G1オッズ帯外(20倍超)'},
        {'date':'2026-04-12','race':'中山11R','horse':'グリーンエナジー','finish':'1着','odds':'7.3倍','reason':'G1オッズ帯上限(7-10の下限付近)で選外'},
    ]


# ============ 14. アラート(閾値超過検知) ===============================
def build_alerts(yearly, live):
    alerts = []
    # 2024 ROI<100%
    y24 = next((y for y in yearly if y['year']==2024), None)
    if y24 and y24['roi_normal'] < 100:
        alerts.append({'level':'warn','title':f'2024 年間ROI {y24["roi_normal"]}% (閾値100%未達)', 'desc':'F1未勝利の単年不振が主因'})
    # 2023 Winsorized-通常 乖離
    y23 = next((y for y in yearly if y['year']==2023), None)
    if y23:
        diff = abs(y23['roi_wins'] - y23['roi_normal'])
        if diff > 15:
            alerts.append({'level':'info','title':f'2023 Winsorized乖離 {diff:.1f}pt', 'desc':f'Winsorized {y23["roi_wins"]}% → 通常 {y23["roi_normal"]}%'})
    # 2026 OOS サンプル不足
    if live['total']['n'] < 50:
        alerts.append({'level':'danger','title':f'2026(OOS) ROI {live["total"]["roi"]}% n={live["total"]["n"]}', 'desc':'ホールドアウトサンプル不足、経過観察必要'})
    return alerts


# ============ 15. システムヘルス ======================================
def build_health(conn):
    health = {}
    try:
        max_date = conn.execute('SELECT MAX(date) FROM results').fetchone()[0]
        health['db_last'] = max_date
    except Exception:
        health['db_last'] = '-'
    # 直近ログ
    logs = PROJ / 'logs'
    def _latest(pattern):
        fs = sorted(glob.glob(str(logs / pattern)))
        return os.path.basename(fs[-1]) if fs else '-'
    health['jvlink_latest'] = _latest('parallel_fetch_*.log')
    health['sat_latest'] = _latest('saturday_preview_*.log')
    health['raceday_latest'] = _latest('race_day_auto_refresh_*.log')
    health['snapshot_exists'] = (PROJ / 'morning_snapshot.json').exists()
    health['gen_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return health


# ============ 16. 今週サマリ(weekend_predictions.json) ================
def build_week_summary():
    wp = PROJ / 'weekend_predictions.json'
    if not wp.exists():
        return {'n':0, 'cost':0}
    try:
        d = json.load(open(wp, encoding='utf-8'))
    except Exception:
        return {'n':0,'cost':0}
    n = 0; cost = 0
    for p in d:
        bt = p.get('buy_type'); sp = p.get('special_horse')
        if bt or sp:
            n += 1
            cost += 1000 if (bt == 'v6_challenge' or sp) else 2000
    return {'n':n, 'cost':cost}


# ============ 17. main: 集計→埋込 =====================================
def main():
    print(f'🐴 NORISHICO Dashboard Build ({datetime.now().strftime("%H:%M:%S")})')
    t0 = datetime.now()

    # config
    committee = json.load(open(CFG/'committee.json', encoding='utf-8'))
    kanban = json.load(open(CFG/'kanban.json', encoding='utf-8'))
    rule_roi_cfg = json.load(open(CFG/'rule_roi.json', encoding='utf-8'))

    # live 2026
    live = aggregate_live_2026()
    print(f'  ✅ 2026実運用集計: n={live["total"]["n"]} ROI={live["total"]["roi"]}% PL={live["total"]["pl"]}円')

    # yearly
    yearly = build_yearly(live)

    # monthly_roi
    monthly_roi = build_monthly_roi(live)

    # reviews + miss stats
    reviews = build_reviews(live)
    miss_types, venue_misses = build_miss_stats(reviews)

    # SQL集計
    conn = sqlite3.connect(str(DB))
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    print(f'  📊 DB集計開始...')
    odds_roi = build_odds_roi(conn)
    blood_rank = build_blood_rank(conn)
    new_sire = build_new_sire(conn)
    style_venue = build_style_venue(conn)
    jockey_venue = build_jockey_venue(conn)
    stable_track = build_stable_track(conn)
    missed = build_missed(conn)
    health = build_health(conn)
    conn.close()
    print(f'  ✅ SQL集計完了 (blood={len(blood_rank)} 帯={len(odds_roi)})')

    # alerts
    alerts = build_alerts(yearly, live)

    # week summary
    week_summary = build_week_summary()

    DATA = {
        'yearly': yearly,
        'odds_roi': odds_roi,
        'ev_hist': EV_HIST,
        'monthly_roi': monthly_roi,
        'hit_roi': HIT_ROI,
        'rule_roi': rule_roi_cfg['versions'],
        'missed': missed,
        'reviews': reviews,
        'miss_types': miss_types,
        'venue_misses': venue_misses,
        'blood_rank': blood_rank,
        'new_sire': new_sire,
        'style_venue': style_venue,
        'jockey_venue': jockey_venue,
        'stable_track': stable_track,
        'members': committee['members'],
        'posts': committee['posts'],
        'kanban': kanban,
        'alerts': alerts,
        'health': health,
        'week_summary': week_summary,
        'live_total': live['total'],
    }

    # テンプレ読込→埋込
    if not TEMPLATE.exists():
        print(f'  ❌ テンプレが存在しません: {TEMPLATE}')
        print(f'     先に dashboard_prototype.html → dashboard_template.html を用意してください')
        return 2
    tmpl = TEMPLATE.read_text(encoding='utf-8')
    data_json = json.dumps(DATA, ensure_ascii=False, default=str)
    out_html = tmpl.replace('{{DATA_JSON}}', data_json)
    OUTPUT.write_text(out_html, encoding='utf-8')

    elapsed = (datetime.now() - t0).total_seconds()
    print(f'\n✅ Built: {OUTPUT.name} ({OUTPUT.stat().st_size:,} bytes, {elapsed:.1f}s)')
    print(f'   DATA keys: {len(DATA)} / reviews: {len(reviews)} / alerts: {len(alerts)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
