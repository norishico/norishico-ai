"""
Phase I-C: オッズ帯別キャリブレーション補正 + EVベース買い判定検証

手順:
1. 2020-2024を訓練データとして (odds_bin, pred_prob) → 実勝率のlookup構築
2. 2025-2026で検証 (ホールドアウト)
3. 補正後の確率でEV計算
4. 様々な (EV閾値, オッズ下限) の組合せで単勝BT
5. v6.6ベースライン (同データの現行honmei買い) と比較
"""
import json
import sys
import io
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

YEARS = [2020, 2021, 2022, 2023, 2024, 2025, 2026]
TRAIN_YEARS = [2020, 2021, 2022, 2023, 2024]
TEST_YEARS = [2025, 2026]

# オッズ帯 (連続値の bin)
ODDS_BINS = [(1.0, 2.0), (2.0, 3.0), (3.0, 4.0), (4.0, 5.0), (5.0, 7.0),
             (7.0, 10.0), (10.0, 15.0), (15.0, 25.0), (25.0, 50.0), (50.0, 200.0)]
# 予測確率 bin (細かく)
PRED_BINS = [(0, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 40), (40, 100)]


def load_races(years):
    recs = []
    for y in years:
        d = json.load(open(f'btv6_{y}.json', encoding='utf-8'))
        for r in d.get('all_races', []):
            wp = r.get('win_prob_pct')
            fin = r.get('honmei_finish')
            od = r.get('honmei_odds') or 0
            if wp is None or fin is None or od <= 0:
                continue
            recs.append({
                'pred': wp / 100.0,
                'win': 1 if fin == 1 else 0,
                'odds': float(od),
                'grade': r.get('grade', '?'),
                'year': y,
            })
    return recs


def odds_bin_key(odds):
    for lo, hi in ODDS_BINS:
        if lo <= odds < hi:
            return (lo, hi)
    return None


def pred_bin_key(pred_pct):
    for lo, hi in PRED_BINS:
        if lo <= pred_pct < hi:
            return (lo, hi)
    return None


def build_calibrator(train_recs):
    """(odds_bin, pred_bin) → 実勝率 の lookup を作成"""
    bucket = defaultdict(lambda: {'n': 0, 'wins': 0})
    for r in train_recs:
        ok = odds_bin_key(r['odds'])
        pk = pred_bin_key(r['pred'] * 100)
        if not ok or not pk:
            continue
        key = (ok, pk)
        bucket[key]['n'] += 1
        bucket[key]['wins'] += r['win']

    calibrator = {}
    for key, v in bucket.items():
        if v['n'] >= 20:  # 最低サンプル
            calibrator[key] = v['wins'] / v['n']

    # フォールバック: オッズ帯のみの平均
    odds_only = defaultdict(lambda: {'n': 0, 'wins': 0})
    for r in train_recs:
        ok = odds_bin_key(r['odds'])
        if ok:
            odds_only[ok]['n'] += 1
            odds_only[ok]['wins'] += r['win']
    odds_fallback = {k: v['wins']/v['n'] for k, v in odds_only.items() if v['n'] >= 30}

    return calibrator, odds_fallback


def calibrated_prob(r, calibrator, odds_fallback):
    ok = odds_bin_key(r['odds'])
    pk = pred_bin_key(r['pred'] * 100)
    if (ok, pk) in calibrator:
        return calibrator[(ok, pk)]
    if ok in odds_fallback:
        return odds_fallback[ok]
    return r['pred']  # 最終フォールバック


def evaluate_config(recs, calibrator, odds_fallback, ev_threshold, odds_lower, cost_per_bet=1000):
    """指定configで買い判定してROI計算 (単勝のみ)"""
    n_bet = 0
    cost = 0
    ret = 0
    wins = 0
    for r in recs:
        if r['odds'] < odds_lower:
            continue
        cp = calibrated_prob(r, calibrator, odds_fallback)
        ev = cp * r['odds']
        if ev < ev_threshold:
            continue
        n_bet += 1
        cost += cost_per_bet
        if r['win']:
            ret += int(r['odds'] * cost_per_bet)
            wins += 1
    profit = ret - cost
    roi = ret / cost * 100 if cost else 0
    return {'n': n_bet, 'cost': cost, 'ret': ret, 'profit': profit, 'roi': roi, 'wins': wins}


def main():
    print('Phase I-C: オッズ帯別キャリブレーション補正検証')
    print('='*80)

    train = load_races(TRAIN_YEARS)
    test = load_races(TEST_YEARS)
    all_recs = load_races(YEARS)
    print(f'訓練 {TRAIN_YEARS}: {len(train)}レース')
    print(f'検証 {TEST_YEARS}: {len(test)}レース')
    print()

    calibrator, odds_fb = build_calibrator(train)
    print(f'較正lookup: {len(calibrator)}セル (odds_bin × pred_bin)')
    print(f'フォールバック: {len(odds_fb)}オッズ帯')
    print()

    # 較正曲線の確認 (訓練データ)
    print('【訓練データ較正結果 (確認)】')
    print(f"{'odds':>12} | {'pred':>8} | {'calib':>8} | {'元EV':>6} | {'較正EV':>6}")
    print('-'*60)
    ODDS_SAMPLE = [1.5, 2.5, 4.0, 6.0, 10.0, 15.0, 30.0, 60.0]
    for o in ODDS_SAMPLE:
        pred_sample = 0.20  # 代表的予測20%
        fake = {'odds': o, 'pred': pred_sample}
        cp = calibrated_prob(fake, calibrator, odds_fb)
        orig_ev = pred_sample * o
        new_ev = cp * o
        print(f'  {o:>10.1f} | {pred_sample*100:>6.1f}% | {cp*100:>6.1f}% | {orig_ev:>5.2f} | {new_ev:>5.2f}')

    print()
    print('='*80)
    print('【EV閾値 × オッズ下限 グリッドサーチ (2025-2026 検証)】')
    print('='*80)

    # v6.6 ベースライン: 実honmei勝率 for comparison
    # but we need v6.6の実際の買い結果から計算するとフェアネス落ちる
    # ここでは同データ (test) で計算
    test_baseline_tansho = evaluate_config(test, {}, {}, ev_threshold=-999, odds_lower=0, cost_per_bet=1000)
    # 全honmeiを買った場合
    total_tansho = test_baseline_tansho
    print(f'全honmei単勝買い(参考): n={total_tansho["n"]}, ROI={total_tansho["roi"]:.1f}%, 損益{total_tansho["profit"]:+,}')
    print()

    # グリッドサーチ
    best = None
    results = []
    for ev_thr in [1.05, 1.15, 1.25, 1.35, 1.50, 1.75, 2.00]:
        for od_lo in [2.0, 3.0, 5.0, 7.0, 10.0]:
            r = evaluate_config(test, calibrator, odds_fb, ev_thr, od_lo)
            results.append({'ev': ev_thr, 'od': od_lo, **r})
            if r['n'] >= 50 and (best is None or r['profit'] > best['profit']):
                best = {'ev': ev_thr, 'od': od_lo, **r}

    # 上位10結果
    results.sort(key=lambda x: -x['profit'])
    print(f"{'EV閾値':>8} {'Odds下限':>10} {'件数':>6} {'勝数':>6} {'ROI':>8} {'損益':>12}")
    print('-'*60)
    for r in results[:15]:
        flag = '🟢' if r['roi'] >= 110 else ('🟡' if r['roi'] >= 100 else '🔴')
        if r['n'] < 30: continue
        print(f'  {r["ev"]:>5.2f} {r["od"]:>8.1f}倍 {r["n"]:>6} {r["wins"]:>6} {r["roi"]:>6.1f}% {r["profit"]:>+11,} {flag}')

    print()
    print('='*80)
    print(f'【最良config (n>=50)】')
    print('='*80)
    if best:
        print(f'  EV閾値: {best["ev"]}')
        print(f'  Odds下限: {best["od"]}倍')
        print(f'  件数: {best["n"]}R')
        print(f'  勝利: {best["wins"]}勝')
        print(f'  ROI: {best["roi"]:.1f}%')
        print(f'  損益: {best["profit"]:+,}円')
    else:
        print('  該当なし')

    # v6.6ベースライン (2025-2026の現行システム結果)
    print()
    print('【v6.6ベースライン (2025-2026 現行システム)】')
    v66_cost = 0
    v66_ret = 0
    v66_n = 0
    for y in TEST_YEARS:
        d = json.load(open(f'btv6_{y}.json', encoding='utf-8'))
        s = d['summary']
        v66_cost += s.get('investment', 0)
        v66_ret += s.get('investment', 0) + s.get('profit', 0)
        v66_n += s.get('n_bet', 0)
    v66_roi = v66_ret / v66_cost * 100 if v66_cost else 0
    print(f'  件数: {v66_n}R')
    print(f'  ROI: {v66_roi:.1f}%')
    print(f'  損益: {v66_ret - v66_cost:+,}円 (単勝+馬連等混合)')


if __name__ == '__main__':
    main()
