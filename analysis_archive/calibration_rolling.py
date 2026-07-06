"""
Phase I-C 追加検証:
1. ローリング検証 (各年の予測時、それ以前の年を訓練)
2. v6.6との公平比較 (単勝のみで比較)
3. クラス別分解
4. ハイブリッド検証 (v6.6 ルール AND/OR 較正EV)
"""
import json, sys, io
from collections import defaultdict
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

YEARS = [2020, 2021, 2022, 2023, 2024, 2025, 2026]
ODDS_BINS = [(1.0,2.0),(2.0,3.0),(3.0,4.0),(4.0,5.0),(5.0,7.0),(7.0,10.0),
             (10.0,15.0),(15.0,25.0),(25.0,50.0),(50.0,200.0)]
PRED_BINS = [(0,10),(10,15),(15,20),(20,25),(25,30),(30,40),(40,100)]


def load_races(years):
    recs = []
    for y in years:
        d = json.load(open(f'btv6_{y}.json', encoding='utf-8'))
        for r in d.get('all_races', []):
            wp = r.get('win_prob_pct')
            fin = r.get('honmei_finish')
            od = r.get('honmei_odds') or 0
            if wp is None or fin is None or od <= 0: continue
            recs.append({
                'pred': wp/100.0,
                'win': 1 if fin == 1 else 0,
                'odds': float(od),
                'grade': r.get('grade','?'),
                'year': y,
                'ev_ok': r.get('ev_ok', False),  # v6.6買い判定
                'gap': r.get('gap', 0) or 0,
                'accel': r.get('honmei_accel', False),
            })
    return recs


def odds_bk(o):
    for lo, hi in ODDS_BINS:
        if lo <= o < hi: return (lo, hi)
    return None

def pred_bk(p):
    for lo, hi in PRED_BINS:
        if lo <= p < hi: return (lo, hi)
    return None


def build_calib(train):
    bucket = defaultdict(lambda: {'n':0,'wins':0})
    odds_only = defaultdict(lambda: {'n':0,'wins':0})
    for r in train:
        ok = odds_bk(r['odds'])
        pk = pred_bk(r['pred']*100)
        if ok and pk:
            bucket[(ok,pk)]['n'] += 1
            bucket[(ok,pk)]['wins'] += r['win']
        if ok:
            odds_only[ok]['n'] += 1
            odds_only[ok]['wins'] += r['win']
    cal = {k: v['wins']/v['n'] for k,v in bucket.items() if v['n'] >= 20}
    fb = {k: v['wins']/v['n'] for k,v in odds_only.items() if v['n'] >= 30}
    return cal, fb


def calib_prob(r, cal, fb):
    ok = odds_bk(r['odds'])
    pk = pred_bk(r['pred']*100)
    if (ok,pk) in cal: return cal[(ok,pk)]
    if ok in fb: return fb[ok]
    return r['pred']


def eval_config(recs, cal, fb, ev_thr, od_lo, cost=1000, use_v66_filter=False):
    n, c, ret, wins = 0, 0, 0, 0
    for r in recs:
        if r['odds'] < od_lo: continue
        if use_v66_filter and not r['ev_ok']: continue
        cp = calib_prob(r, cal, fb)
        ev = cp * r['odds']
        if ev < ev_thr: continue
        n += 1; c += cost
        if r['win']:
            ret += int(r['odds']*cost); wins += 1
    return {'n':n,'wins':wins,'cost':c,'ret':ret,'profit':ret-c,
            'roi':ret/c*100 if c else 0}


def main():
    print('【Phase I-C 追加検証】')
    print('='*80)

    all_recs = load_races(YEARS)
    print(f'全データ: {len(all_recs)}レース')
    print()

    # ──────────────────────────────────────
    # 1. ローリング検証
    # ──────────────────────────────────────
    print('='*80)
    print('1. ローリング検証 (年Y予測時は 2020-(Y-1) で較正学習)')
    print('='*80)
    # 最良config探索 (前回結果 EV≥1.05 & odds≥5 をベース)
    CONFIGS = [
        (1.05, 3.0), (1.05, 5.0), (1.05, 7.0),
        (1.15, 3.0), (1.15, 5.0),
        (1.25, 3.0), (1.25, 5.0),
        (1.35, 3.0), (1.35, 5.0),
    ]

    rolling_totals = defaultdict(lambda: {'n':0,'wins':0,'cost':0,'ret':0})
    for test_year in [2021, 2022, 2023, 2024, 2025, 2026]:
        train_years = [y for y in YEARS if y < test_year]
        if len(train_years) < 2: continue
        train = [r for r in all_recs if r['year'] in train_years]
        test = [r for r in all_recs if r['year'] == test_year]
        cal, fb = build_calib(train)
        for ev_thr, od_lo in CONFIGS:
            res = eval_config(test, cal, fb, ev_thr, od_lo)
            k = f'EV≥{ev_thr}&od≥{od_lo}'
            rolling_totals[k]['n'] += res['n']
            rolling_totals[k]['wins'] += res['wins']
            rolling_totals[k]['cost'] += res['cost']
            rolling_totals[k]['ret'] += res['ret']

    print(f"{'設定':>16} {'件数':>6} {'勝':>5} {'ROI':>8} {'損益':>12}")
    print('-'*60)
    best_rolling = None
    for k, v in sorted(rolling_totals.items(), key=lambda x: -(x[1]['ret']-x[1]['cost'])):
        if v['cost'] == 0: continue
        roi = v['ret']/v['cost']*100
        pr = v['ret']-v['cost']
        flag = '🟢' if roi>=110 else ('🟡' if roi>=100 else '🔴')
        print(f'{k:>16} {v["n"]:>6} {v["wins"]:>5} {roi:>6.1f}% {pr:>+11,} {flag}')
        if v['n'] >= 100 and (best_rolling is None or pr > best_rolling['profit']):
            best_rolling = {'key':k, 'roi':roi, 'profit':pr, **v}

    print()
    print(f'【Rolling 最良 (n>=100)】 {best_rolling["key"] if best_rolling else "なし"}')
    if best_rolling:
        print(f'  件数={best_rolling["n"]}, ROI={best_rolling["roi"]:.1f}%, 損益{best_rolling["profit"]:+,}')

    # ──────────────────────────────────────
    # 2. v6.6 単勝のみ換算での公平比較
    # ──────────────────────────────────────
    print()
    print('='*80)
    print('2. v6.6 単勝のみ換算での比較 (同期間2025-2026)')
    print('='*80)
    test = [r for r in all_recs if r['year'] in [2025, 2026]]
    train = [r for r in all_recs if r['year'] in [2020, 2021, 2022, 2023, 2024]]
    cal, fb = build_calib(train)

    # v6.6 買い判定の馬を単勝1000円で買ったROI
    v66_only = [r for r in test if r['ev_ok']]
    v66_cost = len(v66_only) * 1000
    v66_ret = sum(int(r['odds']*1000) for r in v66_only if r['win'])
    v66_wins = sum(1 for r in v66_only if r['win'])
    v66_roi = v66_ret/v66_cost*100 if v66_cost else 0
    print(f'v6.6 単勝換算: {len(v66_only)}R 勝{v66_wins} ROI{v66_roi:.1f}% 損益{v66_ret-v66_cost:+,}')

    # 較正EV (EV>=1.05, odds>=5)
    calib_res = eval_config(test, cal, fb, 1.05, 5.0)
    print(f'較正EV(≥1.05&≥5): {calib_res["n"]}R 勝{calib_res["wins"]} ROI{calib_res["roi"]:.1f}% 損益{calib_res["profit"]:+,}')

    # ハイブリッド: v6.6買い判定 AND 較正EV>=1.05
    hyb_res = eval_config(test, cal, fb, 1.05, 5.0, use_v66_filter=True)
    print(f'Hybrid(v6.6∧EV≥1.05): {hyb_res["n"]}R 勝{hyb_res["wins"]} ROI{hyb_res["roi"]:.1f}% 損益{hyb_res["profit"]:+,}')

    # ──────────────────────────────────────
    # 3. クラス別の較正効果 (全年, leave-one-out CV風)
    # ──────────────────────────────────────
    print()
    print('='*80)
    print('3. クラス別の較正EV (全年 rolling)')
    print('='*80)
    # 集約: 全年rolling で各クラスのパフォーマンス
    class_agg = defaultdict(lambda: {'n':0,'wins':0,'cost':0,'ret':0})
    for test_year in [2021, 2022, 2023, 2024, 2025, 2026]:
        train_years = [y for y in YEARS if y < test_year]
        train = [r for r in all_recs if r['year'] in train_years]
        test = [r for r in all_recs if r['year'] == test_year]
        cal, fb = build_calib(train)
        for r in test:
            if r['odds'] < 5.0: continue
            cp = calib_prob(r, cal, fb)
            ev = cp * r['odds']
            if ev < 1.05: continue
            g = r['grade']
            class_agg[g]['n'] += 1
            class_agg[g]['cost'] += 1000
            if r['win']:
                class_agg[g]['wins'] += 1
                class_agg[g]['ret'] += int(r['odds']*1000)

    print(f"{'クラス':>8} {'件数':>6} {'勝':>5} {'ROI':>8} {'損益':>12}")
    print('-'*50)
    for g in ['新馬','未勝利','1勝','2勝','3勝','G1','G2','G3']:
        v = class_agg.get(g, {'n':0,'wins':0,'cost':0,'ret':0})
        if v['n'] == 0: continue
        roi = v['ret']/v['cost']*100 if v['cost'] else 0
        pr = v['ret']-v['cost']
        flag = '🟢' if roi>=110 else ('🟡' if roi>=100 else '🔴')
        print(f'{g:>8} {v["n"]:>6} {v["wins"]:>5} {roi:>6.1f}% {pr:>+11,} {flag}')

if __name__ == '__main__':
    main()
