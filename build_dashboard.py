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
    """monthly_results_2026_*.json から2026年の実値を集計。scratched(除外馬)はROI/n計算から除外"""
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
        m = d.get('month','')  # '2026_04' 形式
        mo = 0
        for sep in ('_','-','/'):
            if sep in m:
                try: mo = int(m.split(sep)[1])
                except Exception: pass
                break
        # scratchedを除いて月次集計を再計算
        mo_cost, mo_return, mo_n = 0, 0, 0
        for day in d.get('days', []):
            date = day.get('date','')
            for br in day.get('buy_results', []):
                br_copy = dict(br)
                br_copy['date'] = date
                all_buys.append(br_copy)
                if br.get('miss_type') == 'scratched':
                    continue
                mo_cost += br.get('cost', 0)
                mo_return += br.get('return', 0)
                mo_n += 1
        mo_roi = (mo_return / mo_cost * 100) if mo_cost else 0
        monthly.append({
            'month': mo,
            'roi': round(mo_roi, 1),
            'cost': mo_cost,
            'return': mo_return,
            'n': mo_n,
        })
        total_cost += mo_cost
        total_return += mo_return
        total_n += mo_n

    roi = (total_return / total_cost * 100) if total_cost else 0
    pl = total_return - total_cost
    hits = sum(1 for b in all_buys if b.get('return', 0) > 0 and b.get('miss_type') != 'scratched')
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
def build_monthly_roi(live, conn):
    """2021-2025: JRA全レースの単勝市場平均回収率(SQL実データ)
    2026: 実運用ROI(monthly_results_*.jsonから)
    """
    try:
        rows = conn.execute("""
            SELECT SUBSTR(r.date,1,4) yr, SUBSTR(r.date,6,2) mo,
                   COUNT(*) n,
                   SUM(CASE WHEN r.finish=1 THEN COALESCE(d.tansho_payout,0) ELSE 0 END) payout
            FROM results r LEFT JOIN dividends d ON r.race_id = d.race_id
            WHERE r.date >= '2021-01-01' AND r.date < '2026-01-01'
              AND r.odds IS NOT NULL AND r.odds > 0
            GROUP BY yr, mo
        """).fetchall()
    except Exception as e:
        print(f'  [warn] monthly_roi: {e}')
        rows = []
    result = {}
    for yr, mo, n, payout in rows:
        try:
            yr_i, mo_i = int(yr), int(mo)
        except Exception:
            continue
        if yr_i not in result:
            result[yr_i] = [None]*12
        if n and n > 0:
            roi = payout / (n * 100) * 100
            result[yr_i][mo_i-1] = round(roi, 0)
    rows_out = [{'year':y, 'months':result[y]} for y in sorted(result)]
    # 2026はLive実運用ROI
    months26 = [None]*12
    for m in live['monthly']:
        idx = m['month'] - 1
        if 0 <= idx < 12:
            months26[idx] = round(m['roi'], 0)
    rows_out.append({'year':2026, 'months':months26})
    return rows_out


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
            'review': b.get('review', ''),
            'pace_info': b.get('pace_info'),
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


# ============ 12. EV分布/的中率vsROI(btv6_*.json から動的集計) ========
def build_ev_hist_and_hit_roi():
    from collections import defaultdict
    records, all_races = [], []
    for f in sorted(glob.glob(str(PROJ / 'btv6_2[0-9]*.json'))):
        try:
            d = json.load(open(f, encoding='utf-8'))
        except Exception:
            continue
        records.extend(d.get('bet_records', []))
        for r in d.get('all_races', []):
            if r.get('buy_zone') and r.get('ev7') is not None:
                all_races.append(r)

    # EV_HIST: buy_zone全件のev7をバケット集計
    ev_buckets = defaultdict(int)
    for r in all_races:
        ev = r.get('ev7', 0) or 0
        lo = int(ev // 2) * 2
        ev_buckets[f'{lo}-{lo+2}'] += 1
    ev_hist = [
        {'ev': k, 'n': ev_buckets[k]}
        for k in sorted(ev_buckets, key=lambda x: int(x.split('-')[0]))
    ]

    # HIT_ROI: rule別的中率とROI
    rule_label = {'C2': 'C2新馬', 'F1': 'F1未勝利', '': 'v6_normal', '3rentan': 'G1/G2_3連単'}
    buckets = defaultdict(lambda: {'cost': 0, 'ret': 0, 'n': 0, 'hits': 0})
    for r in records:
        rule = r.get('rule', '')
        if rule.startswith('C2'):       key = 'C2新馬'
        elif rule.startswith('F1'):     key = 'F1未勝利'
        elif rule == '':                key = 'v6_normal'
        elif '3rentan' in rule:         key = 'G1/G2_3連単'
        else:                           key = 'その他'
        buckets[key]['cost'] += r.get('cost', 0)
        buckets[key]['ret']  += r.get('ret', 0)
        buckets[key]['n']    += 1
        if r.get('ret', 0) > 0:
            buckets[key]['hits'] += 1
    order = ['v6_normal', 'C2新馬', 'F1未勝利', 'G1/G2_3連単', 'その他']
    hit_roi = []
    for key in order:
        if key not in buckets:
            continue
        v = buckets[key]
        roi = round(v['ret'] / v['cost'] * 100, 1) if v['cost'] else 0
        hit = round(v['hits'] / v['n'] * 100, 1) if v['n'] else 0
        hit_roi.append({'cls': key, 'hit': hit, 'roi': roi, 'n': v['n']})

    return ev_hist, hit_roi


EV_HIST, HIT_ROI = build_ev_hist_and_hit_roi()


# ============ 13. 取りこぼしログ(見送り好成績) =========================
def build_project_status():
    """dashboard_config/project_status.json を読み込んで返す。なければ generate して返す。"""
    f = CFG / 'project_status.json'
    if not f.exists():
        try:
            from generate_project_status import generate
            return generate('build_dashboard')
        except Exception:
            return {}
    try:
        return json.load(open(f, encoding='utf-8'))
    except Exception:
        return {}


def build_nar_results():
    """NAR実運用成績をnar_predictions/から集計して返す"""
    import glob as _glob
    pred_dir = PROJ / 'nar_predictions'
    files = sorted(_glob.glob(str(pred_dir / 'nar_pred_*.json')))
    records = []
    for fpath in files:
        try:
            preds = json.load(open(fpath, encoding='utf-8'))
            for p in preds:
                if p.get('finish') is not None:
                    records.append(p)
        except Exception:
            pass

    total_n = len(records)
    wins    = [r for r in records if r.get('finish') == 1]
    cost    = total_n * 1000
    ret     = sum(int(r.get('result_odds', 0) * 100) * 10 for r in wins)
    roi     = round(ret / cost * 100, 1) if cost else 0
    pl      = ret - cost

    # 直近10件
    recent = sorted(records, key=lambda x: x.get('date',''), reverse=True)[:10]

    # 現行パラメータ
    params = 'score≥4.5 / odds 5-25倍 / gap≥2.5 / class空欄除外 (v2 2026-04-27)'

    return {
        'total_n': total_n,
        'wins':    len(wins),
        'cost':    cost,
        'ret':     ret,
        'roi':     roi,
        'pl':      pl,
        'recent':  recent,
        'params':  params,
    }


def build_committee_1on1():
    """1on1ログをdashboard_config/committee_1on1_*.jsonから読み込む"""
    import glob as _glob
    files = sorted(_glob.glob(str(CFG / 'committee_1on1_*.json')), reverse=True)
    sessions = []
    for fpath in files:
        try:
            d = json.load(open(fpath, encoding='utf-8'))
            sessions.append(d)
        except Exception:
            pass
    return sessions


def build_committee_comp():
    """委員会 vs AI 対決データを集計して返す。"""
    f = CFG / 'committee_competition.json'
    if not f.exists():
        return {'meta': {}, 'entries': [], 'standings': []}
    d = json.load(open(f, encoding='utf-8'))
    entries = d.get('entries', [])

    # メンバー別累積成績
    from collections import defaultdict
    stats = defaultdict(lambda: {'n': 0, 'cost': 0, 'ret': 0, 'wins': 0})
    for e in entries:
        m = e.get('member', '')
        if not m:
            continue
        ret = e.get('result_ret')
        if ret is None:
            continue  # 未発走は集計しない
        raw_cost = e.get('bet', 2000)
        cost = raw_cost if isinstance(raw_cost, (int, float)) else 1000
        stats[m]['n']    += 1
        stats[m]['cost'] += cost
        stats[m]['ret']  += ret
        if ret > 0:
            stats[m]['wins'] += 1

    standings = []
    for member, s in stats.items():
        roi = round(s['ret'] / s['cost'] * 100, 1) if s['cost'] else 0
        standings.append({
            'member': member,
            'n':      s['n'],
            'wins':   s['wins'],
            'roi':    roi,
            'pl':     s['ret'] - s['cost'],
        })
    standings.sort(key=lambda x: x['roi'], reverse=True)

    return {
        'meta':      d.get('meta', {}),
        'entries':   entries,
        'standings': standings,
    }


def build_scoring_design():
    """スコアリング設計（比重・ボーナス）を返す。scoring.pyの定数と同期。"""
    return {
        'weights_normal': [
            {'name': '過去成績',     'key': 'past_performance', 'pct': 20, 'note': 'タイム・着順・重賞実績'},
            {'name': '調教',         'key': 'training',         'pct': 20, 'note': '前走タイム・加速ラップ（市場非織込★）'},
            {'name': 'コース適性',   'key': 'course_fitness',   'pct': 17, 'note': '同距離・同馬場での過去実績'},
            {'name': '騎手・調教師', 'key': 'jockey_trainer',   'pct': 12, 'note': '騎手複勝率・師弟コンビボーナス'},
            {'name': '血統(父)',      'key': 'sire',             'pct': 10, 'note': '父の会場・距離適性'},
            {'name': '枠順・脚質',   'key': 'gate_style',       'pct':  8, 'note': 'コース別バイアステーブル（市場非織込★）'},
            {'name': 'ローテーション','key': 'rotation',        'pct':  7, 'note': '間隔・前走着順・距離変化'},
            {'name': '血統(母父)',    'key': 'dam_sire',         'pct':  6, 'note': '母父の適性'},
        ],
        'weights_maiden': [
            {'name': '調教',         'key': 'training',         'pct': 33, 'note': '新馬は実績なし→調教が主役'},
            {'name': '騎手・調教師', 'key': 'jockey_trainer',   'pct': 19, 'note': '新馬は騎手の腕が出やすい'},
            {'name': '血統(父)',      'key': 'sire',             'pct': 16, 'note': '新馬は血統で素質を判断'},
            {'name': 'コース適性',   'key': 'course_fitness',   'pct':  9, 'note': ''},
            {'name': '血統(母父)',    'key': 'dam_sire',         'pct':  7, 'note': ''},
            {'name': '枠順・脚質',   'key': 'gate_style',       'pct':  6, 'note': ''},
            {'name': 'ローテーション','key': 'rotation',        'pct':  5, 'note': ''},
            {'name': '過去成績',     'key': 'past_performance', 'pct':  5, 'note': '新馬は過去走なし→最小'},
        ],
        'bonuses': [
            {'name': 'venue_sire_bonus',    'max': '+5pt',  'cond': '会場×父 相性(n≥30, diff≥+12)'},
            {'name': 'venue_damsire_bonus', 'max': '+3pt',  'cond': '会場×母父 相性(係数控えめ)'},
            {'name': 'cushion_sire_bonus',  'max': '+1.5pt','cond': '芝クッション値×父(soft/normal/firm)'},
            {'name': 'nicks_bonus',         'max': '+1.5pt','cond': '父×母父ニックス(n≥100, diff≥+10)'},
            {'name': 'course_blood_bonus',  'max': '+5pt',  'cond': 'コース×血統の特殊相性(芝/ダ別)'},
            {'name': 'gate_cond_blood_bonus','max': '可変', 'cond': '枠順・馬場×血統'},
            {'name': 'track_bias_bonus',    'max': '可変',  'cond': 'バイアスフィルタ(内枠偏重場で見送り)'},
        ],
    }


def build_missed(conn):
    """btv6_*.json の all_races から buy_zone=None かつ honmei が1着だったレースを抽出。"""
    missed = []
    for f in sorted(glob.glob(str(PROJ / 'btv6_*.json'))):
        try:
            d = json.load(open(f, encoding='utf-8'))
        except Exception:
            continue
        for r in d.get('all_races', []):
            if r.get('buy_zone'):
                continue
            if not (r.get('actual_win') or r.get('honmei_finish') == 1):
                continue
            odds = r.get('honmei_odds') or 0
            missed.append({
                'date':   r.get('date', ''),
                'race':   f"{r.get('venue','')}{r.get('race_num','')}R",
                'horse':  r.get('honmei_name', ''),
                'finish': '1着',
                'odds':   f"{odds}倍",
                'reason': r.get('pass_reason') or _missed_reason(r),
            })
    missed.sort(key=lambda x: x['date'], reverse=True)
    return missed[:50]


def _missed_reason(r):
    odds = r.get('honmei_odds') or 0
    ev   = r.get('honmei_ev') or 0
    grade = r.get('grade', '')
    if grade in ('1勝', '2勝'):
        return f'{grade}クラスは対象外'
    if odds < 7:
        return f'オッズ{odds}倍: 買い帯域外'
    if ev < 2.0:
        return f'EV{ev:.1f}: 基準未満'
    return '買い条件不成立'


# ============ 14a. 券種別ROI (btv6_YYYY.json から集計) ==================
WINSORIZE_CAP = 50000  # backtest_v6.py と同じ

def build_bet_type_roi():
    """btv6_*.json から bet_records を読んで券種別に集計

    rule 分類:
      - '3rentan_box' = 3連単BOX
      - 'C2_新馬accel' = C2新馬チャレンジ
      - 'F1_未勝利主流accel' = F1未勝利チャレンジ
      - その他 = v6本体(単勝+馬連)
    """
    categories = {
        'v6_main':   {'label':'v6本体 (単勝+馬連)', 'records':[]},
        'c2':        {'label':'C2 新馬', 'records':[]},
        'f1':        {'label':'F1 未勝利', 'records':[]},
        'sanrentan': {'label':'3連単 BOX', 'records':[]},
    }
    all_records = []
    years = []
    for f in sorted(glob.glob(str(PROJ / 'btv6_*.json'))):
        try:
            d = json.load(open(f, encoding='utf-8'))
        except Exception:
            continue
        y = os.path.basename(f).replace('btv6_','').replace('.json','')
        years.append(y)
        for b in d.get('bet_records', []):
            rule = b.get('rule','')
            all_records.append(b)
            if rule == '3rentan_box':
                categories['sanrentan']['records'].append(b)
            elif rule == 'C2_新馬accel':
                categories['c2']['records'].append(b)
            elif rule == 'F1_未勝利主流accel':
                categories['f1']['records'].append(b)
            else:
                categories['v6_main']['records'].append(b)

    def _stats(records):
        n = len(records)
        cost = sum((b.get('cost',0) or 0) for b in records)
        ret = sum((b.get('ret',0) or 0) for b in records)
        ret_wins = sum(min((b.get('ret',0) or 0), WINSORIZE_CAP) for b in records)
        hits = sum(1 for b in records if (b.get('ret',0) or 0) > 0)
        roi = (ret / cost * 100) if cost else 0
        roi_wins = (ret_wins / cost * 100) if cost else 0
        mega_count = sum(1 for b in records if (b.get('ret',0) or 0) > WINSORIZE_CAP)
        return {
            'n':n, 'cost':cost, 'ret':ret, 'pl':ret-cost,
            'roi':round(roi,1), 'roi_wins':round(roi_wins,1),
            'hit':hits, 'hit_rate':round(hits/n*100,1) if n else 0,
            'mega':mega_count,
        }

    result = {
        'period': f"{years[0]}-{years[-1]}" if years else '-',
        'total': _stats(all_records),
    }
    for k, v in categories.items():
        result[k] = _stats(v['records'])
        result[k]['label'] = v['label']
    return result


# ============ 14b. 月別BT分析 ==========================================
def build_monthly_bt():
    """btv6_*.json の bet_records から我々のモデルの月別BT成績を集計 (2022-2025)。
    bet_records は実際に賭けたレースのみ。all_races は全候補で混在するので使わない。
    Winsorized ROI (50k cap) も併記してメガヒット依存を可視化する。
    """
    result = {}  # {year: {month: {n, cost, ret, ret_w}}}
    for f in sorted(glob.glob(str(PROJ / 'btv6_*.json'))):
        try:
            d = json.load(open(f, encoding='utf-8'))
        except Exception:
            continue
        yr_s = os.path.basename(f).replace('btv6_', '').replace('.json', '')
        try:
            yr = int(yr_s)
        except ValueError:
            continue
        if yr not in result:
            result[yr] = {}
        for r in d.get('bet_records', []):
            date_s = r.get('date', '')
            try:
                mo = int(date_s[5:7])
            except (ValueError, TypeError, IndexError):
                continue
            if mo not in result[yr]:
                result[yr][mo] = {'n': 0, 'cost': 0, 'ret': 0, 'ret_w': 0}
            e = result[yr][mo]
            ret = r.get('ret', 0) or 0
            cost = r.get('cost', 0) or 0
            e['n']     += 1
            e['cost']  += cost
            e['ret']   += ret
            e['ret_w'] += min(ret, WINSORIZE_CAP)

    rows = []
    for yr in sorted(result):
        months = []
        for mo in range(1, 13):
            e = result[yr].get(mo)
            if e and e['cost'] > 0:
                roi   = round(e['ret']   / e['cost'] * 100, 1)
                roi_w = round(e['ret_w'] / e['cost'] * 100, 1)
                months.append({'n': e['n'], 'cost': e['cost'], 'ret': e['ret'], 'roi': roi, 'roi_w': roi_w})
            else:
                months.append(None)
        rows.append({'year': yr, 'months': months})
    return rows


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
        fs = glob.glob(str(logs / pattern))
        return os.path.basename(max(fs, key=os.path.getmtime)) if fs else '-'
    health['jvlink_latest'] = _latest('parallel_fetch_*.log')
    health['sat_latest'] = _latest('saturday_preview_*.log')
    health['raceday_latest'] = _latest('race_day_auto_refresh_*.log')
    health['snapshot_exists'] = (PROJ / 'morning_snapshot.json').exists()
    health['gen_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return health


# ============ 16. Phase II進捗集計 ================
def build_phase2_progress(live):
    """Phase II移行条件の進捗を集計 (2026-04-22委員会決定)
    条件: N>=100 AND 累積ROI>=100% AND 4季節経験
    """
    n = live['total']['n']
    roi = live['total']['roi']

    # 経験済み季節の判定 (monthly_results から月を抽出)
    season_months = set()
    files = sorted(glob.glob(str(PROJ / 'monthly_results_2026_*.json')))
    for f in files:
        try:
            d = json.load(open(f, encoding='utf-8'))
            for day in d.get('days', []):
                dt = day.get('date', '')
                if len(dt) >= 7:
                    mo = int(dt[5:7])
                    season_months.add(mo)
        except Exception:
            pass

    seasons = {
        'spring': any(m in season_months for m in [3, 4, 5]),
        'summer': any(m in season_months for m in [6, 7, 8]),
        'autumn': any(m in season_months for m in [9, 10, 11]),
        'winter': any(m in season_months for m in [12, 1, 2]),
    }
    seasons_done = sum(seasons.values())

    return {
        'n': n, 'n_target': 100,
        'roi': roi, 'roi_target': 100,
        'seasons': seasons, 'seasons_done': seasons_done,
        'all_clear': n >= 100 and roi >= 100 and seasons_done >= 4,
    }


# ============ 17. 今週サマリ(weekend_predictions.json) ================
def build_week_summary():
    wp = PROJ / 'weekend_predictions.json'
    if not wp.exists():
        return {'n':0, 'cost':0, 'date_label':''}
    try:
        d = json.load(open(wp, encoding='utf-8'))
    except Exception:
        return {'n':0, 'cost':0, 'date_label':''}
    n = 0; cost = 0
    for p in d:
        bt = p.get('buy_type'); sp = p.get('special_horse')
        if bt or sp:
            n += 1
            cost += 1000 if (bt == 'v6_challenge' or sp) else 2000
    from datetime import date, timedelta
    today = date.today()
    days_since_sat = (today.weekday() - 5) % 7
    sat = today - timedelta(days=days_since_sat)
    sun = sat + timedelta(days=1)
    date_label = f'{sat.month}/{sat.day}(Sat)+{sun.month}/{sun.day}(Sun)'
    return {'n':n, 'cost':cost, 'date_label':date_label}


# ============ 17. 週次自動化スケジュール ==================================
def build_schedule():
    """週次自動化タスクのスケジュール一覧を返す"""
    return [
        {'day': '月〜木', 'time': '20:00', 'task': '調教データ取込',
         'detail': 'TFJV DAT → keiba.db training table',
         'script': 'training_import.bat', 'color': 'blue'},
        {'day': '金', 'time': '19:00', 'task': '土曜予想生成',
         'detail': '枠確定 → スコアリング → weekend_predictions.json → dashboard更新',
         'script': 'saturday_preview.bat', 'color': 'purple'},
        {'day': '土', 'time': '09:00', 'task': '朝確認・買いGO判定',
         'detail': 'オッズ取得 → ±20%チェック → 買いGO / 見送り Discord通知 → dashboard.html再生成',
         'script': 'race_day_auto_refresh.bat (--once)', 'color': 'green'},
        {'day': '土', 'time': '各発走10分前', 'task': '直前オッズ確認+ロック',
         'detail': 'auto_refresh.py ループ: オッズ変動±20%でロック / discord通知',
         'script': 'auto_refresh.py', 'color': 'green'},
        {'day': '土', 'time': '20:00', 'task': '日曜予想生成',
         'detail': '土+日両日再生成 → dashboard更新',
         'script': 'sunday_preview.bat', 'color': 'purple'},
        {'day': '日', 'time': '09:00', 'task': '朝確認・買いGO判定',
         'detail': 'オッズ取得 → ±20%チェック → 買いGO / 見送り Discord通知 → dashboard.html再生成',
         'script': 'race_day_auto_refresh.bat --sunday (--once)', 'color': 'green'},
        {'day': '日', 'time': '各発走10分前', 'task': '直前オッズ確認+ロック',
         'detail': 'auto_refresh.py --sunday ループ',
         'script': 'auto_refresh.py --sunday', 'color': 'green'},
        {'day': '日', 'time': '全レース終了後', 'task': '結果保存・委員会対決更新・Dashboard再生成',
         'detail': 'save_results.py → monthly_results更新 → committee_competition.json更新 → build_dashboard.py',
         'script': 'publish_weekend.py --save-results', 'color': 'gold'},
    ]


# ============ 18. main: 集計→埋込 =====================================
def main():
    print(f'🐴 NORISHICO Dashboard Build ({datetime.now().strftime("%H:%M:%S")})')
    t0 = datetime.now()

    # config
    committee = json.load(open(CFG/'committee.json', encoding='utf-8'))
    kanban = json.load(open(CFG/'kanban.json', encoding='utf-8'))
    rule_roi_cfg = json.load(open(CFG/'rule_roi.json', encoding='utf-8'))
    rejected_ideas = json.load(open(CFG/'rejected_ideas.json', encoding='utf-8')) if (CFG/'rejected_ideas.json').exists() else []

    # live 2026
    live = aggregate_live_2026()
    print(f'  ✅ 2026実運用集計: n={live["total"]["n"]} ROI={live["total"]["roi"]}% PL={live["total"]["pl"]}円')

    # yearly
    yearly = build_yearly(live)

    # monthly_roi (DB集計必要なので conn 渡す、conn 開くのはこの直後なので順序調整)

    # reviews + miss stats
    reviews = build_reviews(live)
    miss_types, venue_misses = build_miss_stats(reviews)

    # SQL集計
    conn = sqlite3.connect(str(DB))
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    print(f'  📊 DB集計開始...')
    monthly_roi = build_monthly_roi(live, conn)
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

    # Phase II progress
    phase2 = build_phase2_progress(live)

    # 券種別ROI(btv6_*.json集計)
    bet_type_roi = build_bet_type_roi()
    print(f'  ✅ 券種別ROI: 全体 {bet_type_roi["total"]["n"]}R ROI={bet_type_roi["total"]["roi"]}% / 3連単 {bet_type_roi["sanrentan"]["n"]}R ROI={bet_type_roi["sanrentan"]["roi"]}%')

    # 月別BT成績
    monthly_bt = build_monthly_bt()
    print(f'  ✅ 月別BT: {len(monthly_bt)}年分')

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
        'bet_type_roi': bet_type_roi,
        'phase2': phase2,
        'rejected_ideas': rejected_ideas,
        'scoring_design': build_scoring_design(),
        'committee_comp': build_committee_comp(),
        'committee_1on1': build_committee_1on1(),
        'nar_results':    build_nar_results(),
        'schedule': build_schedule(),
        'monthly_bt': monthly_bt,
        'project_status': build_project_status(),
        'build_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
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
