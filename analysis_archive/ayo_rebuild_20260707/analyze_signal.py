# -*- coding: utf-8 -*-
"""
AYO再構築② MCシグナルの統計的検証（leak-free / seeded N_MC=1000 データセット使用）

出力:
 1. MCキャリブレーション（予測top3率 vs 実測top3率）
 2. 人気層内リフト（人気で層別したときMCが追加情報を持つか）
 3. 軸的中率: MC top1 vs 1番人気 vs ランダム（Wilson 95%CI）
 4. 戦略ROI + クラスタブートストラップ95%CI（レース単位リサンプル）
 5. スライス探索（ダ/芝 × 距離帯 × MC閾値）+ Benjamini-Hochberg補正
"""
import os
import pickle
import sys

import numpy as np

sys.stdout.reconfigure(encoding='utf-8')
SCRATCH = os.path.dirname(os.path.abspath(__file__))
rng = np.random.default_rng(7)

INVEST_UW = 400  # 馬連1+ワイド3
N_BOOT = 5000

with open(os.path.join(SCRATCH, 'mc_dataset.pkl'), 'rb') as f:
    DATA = pickle.load(f)
races = DATA['races']
print(f"データ: {len(races):,}R  N_MC={DATA['n_mc']} seed={DATA['seed']}  期間{DATA['bt_start']}..{DATA['bt_end']}\n")


def wilson_ci(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


# ============================================================
# 1. キャリブレーション
# ============================================================
print('=' * 60)
print('1. MCキャリブレーション（全馬、芝+ダ）')
print('=' * 60)
bins = [(i / 10, (i + 1) / 10) for i in range(10)]
cal = {b: [0, 0] for b in bins}
for r in races:
    for h in r['horses']:
        fin = h['finish']
        if fin is None:
            continue
        hit = float(fin) <= 3.0
        for b in bins:
            if b[0] <= h['mc'] < b[1] or (b[1] == 1.0 and h['mc'] == 1.0):
                cal[b][0] += 1
                cal[b][1] += hit
                break
print(f'{"MC帯":>10} {"n":>8} {"予測中央":>8} {"実測top3率":>10} {"実測95%CI":>18}')
for b in bins:
    n, k = cal[b]
    if n < 50:
        continue
    lo, hi = wilson_ci(k, n)
    print(f'{b[0]:.1f}-{b[1]:.1f} {n:>8,} {(b[0]+b[1])/2:>8.2f} {k/n:>10.3f}   [{lo:.3f}, {hi:.3f}]')

# ============================================================
# 2. 人気層内リフト（MCは市場に対して追加情報を持つか）
# ============================================================
print()
print('=' * 60)
print('2. 人気層内リフト: 層内でMC上位半分 vs 下位半分の実測top3率と複勝ROI')
print('   （MCが市場情報の再包装なら差はゼロに近いはず）')
print('=' * 60)
strata = [(1, 3), (4, 6), (7, 9), (10, 99)]


def fukusho_payout(r, umaban):
    d = r['div']
    if d is None or umaban is None:
        return 0
    # d: tansho(0,1), fukusho1(2,3), fukusho2(4,5), fukusho3(6,7), umaren(8,9,10), wide...(11..19)
    for ui, pi in ((2, 3), (4, 5), (6, 7)):
        if d[ui] == umaban and d[pi]:
            return d[pi]
    return 0


print(f'{"人気層":>8} {"MC側":>6} {"n":>8} {"top3率":>8} {"95%CI":>18} {"複勝ROI":>8}')
for lo_p, hi_p in strata:
    for side in ('high', 'low'):
        n = k = inv = ret = 0
        for r in races:
            grp = [h for h in r['horses']
                   if h['popularity'] and lo_p <= h['popularity'] <= hi_p and h['finish'] is not None]
            if len(grp) < 2:
                continue
            grp.sort(key=lambda h: -h['mc'])
            half = len(grp) // 2
            sel = grp[:half] if side == 'high' else grp[half:]
            for h in sel:
                n += 1
                k += float(h['finish']) <= 3.0
                inv += 100
                ret += fukusho_payout(r, h['umaban'])
        cl, ch = wilson_ci(k, n)
        roi = ret / inv * 100 if inv else 0
        print(f'{lo_p}-{hi_p:>2}人気 {side:>6} {n:>8,} {k/n:>8.3f}   [{cl:.3f}, {ch:.3f}] {roi:>7.1f}%')

# ============================================================
# 3. 軸的中率（top3内率）: MC top1 vs 1番人気 vs ランダム
# ============================================================
print()
print('=' * 60)
print('3. 軸的中率（軸馬が3着以内に来る率、芝+ダ / ダのみ）')
print('=' * 60)


def axis_hits(races_sub, mode):
    n = k = 0
    for r in races_sub:
        hs = [h for h in r['horses'] if h['finish'] is not None]
        if len(hs) < 4:
            continue
        if mode == 'mc':
            ax = max(hs, key=lambda h: h['mc'])
        elif mode == 'pop':
            cands = [h for h in hs if h['popularity']]
            if not cands:
                continue
            ax = min(cands, key=lambda h: h['popularity'])
        else:  # random
            ax = hs[rng.integers(len(hs))]
        n += 1
        k += float(ax['finish']) <= 3.0
    return n, k


for label, sub in (('芝+ダ', races), ('ダのみ', [r for r in races if r['surface'] == 'ダ'])):
    print(f'-- {label} --')
    for mode, name in (('mc', 'MC top1'), ('pop', '1番人気'), ('rand', 'ランダム')):
        n, k = axis_hits(sub, mode)
        lo, hi = wilson_ci(k, n)
        print(f'  {name:>8}: n={n:,}  top3内率={k/n:.3f}  95%CI[{lo:.3f}, {hi:.3f}]')

# ============================================================
# 4. 戦略ROI + クラスタブートストラップCI
# ============================================================
print()
print('=' * 60)
print(f'4. 戦略ROI（ダートのみ、400円/R=馬連1+ワイド3 / 複勝は100円）+ 95%ブートストラップCI (B={N_BOOT})')
print('=' * 60)


def uw_payout(r, jiku, aite):
    """馬連(軸-相手1) + ワイド(軸-相手1..3) 各100円"""
    d = r['div']
    if d is None:
        return 0
    pay = 0
    if d[8] is not None and {d[8], d[9]} == {jiku, aite[0]} and d[10]:
        pay += d[10]
    for a in aite:
        pair = {jiku, a}
        for ui1, ui2, pi in ((11, 12, 13), (14, 15, 16), (17, 18, 19)):
            if d[ui1] is not None and {d[ui1], d[ui2]} == pair and d[pi]:
                pay += d[pi]
                break
    return pay


def eval_strategy(races_sub, axis_mode, partner_mode, bet):
    """returns per-race pnl array (invest, ret)"""
    rows = []
    for r in races_sub:
        hs = [h for h in r['horses'] if h['umaban'] is not None]
        if len(hs) < 4:
            continue
        hs_mc = sorted(hs, key=lambda h: -h['mc'])
        if axis_mode == 'mc':
            ax = hs_mc[0]
        elif axis_mode == 'pop':
            cands = [h for h in hs if h['popularity']]
            if not cands:
                continue
            ax = min(cands, key=lambda h: h['popularity'])
        else:
            ax = hs[rng.integers(len(hs))]

        rest = [h for h in hs if h is not ax]
        if partner_mode == 'mc':
            partners = sorted(rest, key=lambda h: -h['mc'])[:3]
        elif partner_mode == 'pop':
            cands = [h for h in rest if h['popularity']]
            if len(cands) < 3:
                continue
            partners = sorted(cands, key=lambda h: h['popularity'])[:3]
        else:
            idx = rng.permutation(len(rest))[:3]
            partners = [rest[i] for i in idx]
        if len(partners) < 3:
            continue

        if bet == 'uw':
            inv = INVEST_UW
            ret = uw_payout(r, ax['umaban'], [p['umaban'] for p in partners])
        else:  # fukusho on axis
            inv = 100
            ret = fukusho_payout(r, ax['umaban'])
        rows.append((inv, ret))
    return np.array(rows, dtype=float)


def boot_roi(rows, n_boot=N_BOOT):
    n = len(rows)
    roi = rows[:, 1].sum() / rows[:, 0].sum() * 100
    idx = rng.integers(0, n, size=(n_boot, n))
    inv_b = rows[idx, 0].sum(axis=1)
    ret_b = rows[idx, 1].sum(axis=1)
    rois = ret_b / inv_b * 100
    return roi, np.percentile(rois, 2.5), np.percentile(rois, 97.5), (rois >= 100).mean()


dirt = [r for r in races if r['surface'] == 'ダ']
configs = [
    ('A: 軸MC1/相手MC2-4/馬連+W', 'mc', 'mc', 'uw'),
    ('B: 軸MC1/相手人気3/馬連+W', 'mc', 'pop', 'uw'),
    ('C: 軸人気1/相手人気2-4/馬連+W', 'pop', 'pop', 'uw'),
    ('D: 軸MC1/複勝のみ', 'mc', 'mc', 'fuku'),
    ('E: 軸人気1/複勝のみ', 'pop', 'pop', 'fuku'),
]
print(f'{"構成":<28} {"n":>6} {"ROI":>7} {"95%CI":>20} {"P(ROI≥100%)":>12}')
for name, am, pm, bet in configs:
    rows = eval_strategy(dirt, am, pm, bet)
    roi, lo, hi, p100 = boot_roi(rows)
    print(f'{name:<28} {len(rows):>6,} {roi:>6.1f}% [{lo:>6.1f}%, {hi:>6.1f}%] {p100:>11.4f}')

# ランダム軸ベースライン（50回平均）
rois = []
for _ in range(50):
    rows = eval_strategy(dirt, 'rand', 'rand', 'uw')
    rois.append(rows[:, 1].sum() / rows[:, 0].sum() * 100)
print(f'{"F: ランダム軸+相手/馬連+W(50回)":<28} {"":>6} {np.mean(rois):>6.1f}% ±{np.std(rois):.1f}  '
      f'[min {np.min(rois):.1f}%, max {np.max(rois):.1f}%]')

# ============================================================
# 5. スライス探索 + BH補正（構成Aで利益が出るスライスは存在するか）
# ============================================================
print()
print('=' * 60)
print('5. スライス探索（構成A）: 面×距離帯×MC1閾値、H0: ROI<=100% の片側ブートストラップp値 + BH補正')
print('=' * 60)


def slice_races(surface, dband, mcmin):
    out = []
    for r in races:
        if surface != 'both' and r['surface'] != surface:
            continue
        dst = r['distance'] or 0
        if dband == 'short' and not dst <= 1400:
            continue
        if dband == 'mid' and not (1400 < dst <= 1800):
            continue
        if dband == 'long' and not dst > 1800:
            continue
        if mcmin > 0:
            hs = [h for h in r['horses'] if h['umaban'] is not None]
            if not hs or max(h['mc'] for h in hs) < mcmin:
                continue
        out.append(r)
    return out


tests = []
for surface in ('ダ', '芝'):
    for dband in ('short', 'mid', 'long'):
        for mcmin in (0.0, 0.5, 0.6):
            sub = slice_races(surface, dband, mcmin)
            rows = eval_strategy(sub, 'mc', 'mc', 'uw')
            if len(rows) < 100:
                continue
            n = len(rows)
            roi = rows[:, 1].sum() / rows[:, 0].sum() * 100
            idx = rng.integers(0, n, size=(2000, n))
            rois = rows[idx, 1].sum(axis=1) / rows[idx, 0].sum(axis=1) * 100
            pval = (rois <= 100).mean()  # H0: 真のROI<=100 → p = P(boot ROI <= 100)は近似
            tests.append((f'{surface}/{dband}/MC>={mcmin}', n, roi, pval))

tests.sort(key=lambda t: t[3])
m = len(tests)
print(f'{"スライス":<22} {"n":>6} {"ROI":>7} {"p(片側)":>8} {"BH有意(q=0.05)":>14}')
any_sig = False
for i, (name, n, roi, pval) in enumerate(tests, 1):
    sig = pval <= 0.05 * i / m
    any_sig |= sig
    print(f'{name:<22} {n:>6,} {roi:>6.1f}% {pval:>8.4f} {"★" if sig else "-":>10}')
print(f'\nBH補正後に有意なスライス: {"あり" if any_sig else "なし"}')
