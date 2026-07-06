"""
Option B: 3勝クラス限定の較正補助検証

現v6.6の3勝 normal 買い候補(既にaccel必須+8-11倍で絞り込み済み)に対して、
さらに較正EV >= X 条件を追加した場合の効果を測定。

慎重検証:
1. 3勝 buy_records のみ抽出
2. 各年でローリング較正
3. EV閾値グリッドサーチ
4. v6.6 ベース vs v6.6 + 較正EV フィルタの比較
"""
import json, sys, io
from collections import defaultdict
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

YEARS = [2020,2021,2022,2023,2024,2025,2026]
ODDS_BINS = [(1.0,2.0),(2.0,3.0),(3.0,4.0),(4.0,5.0),(5.0,7.0),(7.0,10.0),
             (10.0,15.0),(15.0,25.0),(25.0,50.0),(50.0,200.0)]
PRED_BINS = [(0,10),(10,15),(15,20),(20,25),(25,30),(30,40),(40,100)]

def odds_bk(o):
    for lo, hi in ODDS_BINS:
        if lo <= o < hi: return (lo, hi)
    return None

def pred_bk(p):
    for lo, hi in PRED_BINS:
        if lo <= p < hi: return (lo, hi)
    return None


def load_all_races(years):
    """all_racesから全レース (較正学習用)"""
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
                'year': y,
                'grade': r.get('grade','?'),
                'ev_ok': r.get('ev_ok', False),
            })
    return recs


def load_3sho_bets(years):
    """3勝 normal の実際の買い候補を取得"""
    recs = []
    for y in years:
        d = json.load(open(f'btv6_{y}.json', encoding='utf-8'))
        for b in d.get('bet_records', []):
            if b.get('grade') != '3勝': continue
            if b.get('buy_zone') != 'normal': continue
            recs.append({
                'date': b.get('date'),
                'venue': b.get('venue'),
                'race_num': b.get('race_num'),
                'year': y,
                'odds': float(b.get('honmei_odds', 0) or 0),
                'pred': float(b.get('win_prob_pct', 0) or 0) / 100.0,
                'win': 1 if b.get('honmei_finish') == 1 else 0,
                'cost': b.get('cost', 2000),
                'ret': b.get('ret', 0),
                'profit': b.get('ret', 0) - b.get('cost', 2000),
            })
    return recs


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


def main():
    print('【Option B: 3勝クラス限定 較正補助検証】')
    print('='*80)

    all_races = load_all_races(YEARS)
    three_bets = load_3sho_bets(YEARS)
    print(f'全レース: {len(all_races)}, 3勝buy_records: {len(three_bets)}')
    print()

    # v6.6 ベースライン(3勝 normal のみ)
    base_n = len(three_bets)
    base_cost = sum(b['cost'] for b in three_bets)
    base_ret = sum(b['ret'] for b in three_bets)
    base_profit = base_ret - base_cost
    base_wins = sum(1 for b in three_bets if b['win'])
    base_roi = base_ret / base_cost * 100 if base_cost else 0
    print(f'v6.6ベース (3勝normal全て):')
    print(f'  {base_n}R 勝{base_wins} 投資{base_cost:,} 回収{base_ret:,} ROI{base_roi:.1f}% 損益{base_profit:+,}')
    print()

    # 較正補助: 各bet_recordに対してローリング較正prob計算
    print('【ローリング較正で各3勝betのEV計算】')
    print('='*80)
    enriched = []
    for b in three_bets:
        # その年以前を訓練 (少なくとも2020年+)
        train = [r for r in all_races if r['year'] < b['year'] and r['year'] >= 2020]
        if len(train) < 500:
            # 訓練不足なら較正なし、pred そのまま
            cp = b['pred']
        else:
            cal, fb = build_calib(train)
            cp = calib_prob(b, cal, fb)
        ev = cp * b['odds']
        enriched.append({**b, 'calib_prob': cp, 'calib_ev': ev})

    # EV分布
    print('EV分布:')
    ev_ranges = [(0,0.8),(0.8,1.0),(1.0,1.2),(1.2,1.5),(1.5,2.0),(2.0,99)]
    for lo, hi in ev_ranges:
        sub = [b for b in enriched if lo <= b['calib_ev'] < hi]
        if not sub: continue
        n = len(sub)
        c = sum(b['cost'] for b in sub)
        r_ = sum(b['ret'] for b in sub)
        w = sum(1 for b in sub if b['win'])
        roi = r_/c*100 if c else 0
        flag = '🟢' if roi>=110 else ('🟡' if roi>=100 else '🔴')
        print(f'  calib_EV {lo:.1f}-{hi:.1f}: n={n:>3} 勝{w} ROI{roi:>6.1f}% 損益{r_-c:>+8,} {flag}')

    print()
    print('【EV閾値で絞った場合の損益】')
    print('='*80)
    for ev_thr in [0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3]:
        sub = [b for b in enriched if b['calib_ev'] >= ev_thr]
        if not sub: continue
        n = len(sub)
        c = sum(b['cost'] for b in sub)
        r_ = sum(b['ret'] for b in sub)
        w = sum(1 for b in sub if b['win'])
        roi = r_/c*100 if c else 0
        diff_profit = (r_-c) - base_profit * (n/base_n if base_n else 0)  # 比例期待との差
        flag = '🟢' if roi > base_roi else '🔴'
        print(f'  EV>={ev_thr}: n={n:>3} 勝{w} 投資{c:,} ROI{roi:>6.1f}% 損益{r_-c:>+8,} (vs base ROI {base_roi:.1f}%) {flag}')

    # 年別に EV>=1.0 で絞った場合の検証
    print()
    print('【EV>=1.0 絞込の年別検証】')
    print('='*80)
    for ev_thr in [1.0, 1.1]:
        print(f'  EV閾値 {ev_thr}:')
        for y in YEARS:
            year_sub = [b for b in enriched if b['year']==y and b['calib_ev']>=ev_thr]
            year_base = [b for b in three_bets if b['year']==y]
            if not year_base: continue
            if year_sub:
                n_s = len(year_sub)
                c_s = sum(b['cost'] for b in year_sub)
                r_s = sum(b['ret'] for b in year_sub)
                w_s = sum(1 for b in year_sub if b['win'])
                prof_s = r_s - c_s
            else:
                n_s = c_s = r_s = w_s = prof_s = 0
            n_b = len(year_base)
            c_b = sum(b['cost'] for b in year_base)
            r_b = sum(b['ret'] for b in year_base)
            prof_b = r_b - c_b
            diff = prof_s - prof_b
            print(f'    {y}: base {n_b}R/{prof_b:+,} vs filtered {n_s}R/{prof_s:+,} (diff {diff:+,})')

if __name__ == '__main__':
    main()
