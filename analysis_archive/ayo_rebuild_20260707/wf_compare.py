# -*- coding: utf-8 -*-
"""
AYO再構築③ 真のwalk-forward比較

設計（事前登録・全81構成、結果を見てからの追加はしない）:
  軸 = MC最高スコア馬（固定。本検証の対象ファミリー）
  相手 ∈ {MC2-4位, 人気上位3, MC2位+人気上位2}
  面 ∈ {ダ, 芝, 両方}
  軸MC閾値 ∈ {なし, >=0.5, >=0.6}
  券種 ∈ {馬連1+ワイド3(400円), ワイド3点(300円), 複勝軸(100円)}

WF手順: テスト年Y∈{2022,2023,2024,2025,2026H1}。学習=2020..Y-1（拡大窓）で
最良構成（学習ROI最大、n>=200）を選定 → Yに適用。全テスト年のOOS損益を合算。

比較対象:
  - WF-OOS（正直な成績 = 「この選定手続き自体」の期待成績）
  - 固定構成A（現行AYO: ダ/相手MC/馬連+W/閾値なし）のOOS
  - オラクル（各テスト年で事後的に最良構成を選んだ場合 = in-sample上限、参考値）
"""
import os
import pickle
import sys
from itertools import product

import numpy as np

sys.stdout.reconfigure(encoding='utf-8')
SCRATCH = os.path.dirname(os.path.abspath(__file__))
rng = np.random.default_rng(11)

with open(os.path.join(SCRATCH, 'mc_dataset.pkl'), 'rb') as f:
    DATA = pickle.load(f)
races = DATA['races']

TEST_YEARS = ['2022', '2023', '2024', '2025', '2026']
N_MIN_TRAIN = 200


def fukusho_payout(div, umaban):
    if div is None or umaban is None:
        return 0
    for ui, pi in ((2, 3), (4, 5), (6, 7)):
        if div[ui] == umaban and div[pi]:
            return div[pi]
    return 0


def uw_payout(div, jiku, aite, include_umaren=True):
    if div is None:
        return 0
    pay = 0
    if include_umaren and div[8] is not None and {div[8], div[9]} == {jiku, aite[0]} and div[10]:
        pay += div[10]
    for a in aite:
        pair = {jiku, a}
        for ui1, ui2, pi in ((11, 12, 13), (14, 15, 16), (17, 18, 19)):
            if div[ui1] is not None and {div[ui1], div[ui2]} == pair and div[pi]:
                pay += div[pi]
                break
    return pay


def eval_config(races_sub, partner, surface, mcmin, bet):
    rows = []
    for r in races_sub:
        if surface != 'both' and r['surface'] != surface:
            continue
        hs = [h for h in r['horses'] if h['umaban'] is not None]
        if len(hs) < 4:
            continue
        hs_mc = sorted(hs, key=lambda h: (-h['mc'], h['umaban']))
        ax = hs_mc[0]
        if mcmin > 0 and ax['mc'] < mcmin:
            continue
        rest = hs_mc[1:]

        if partner == 'mc':
            partners = rest[:3]
        elif partner == 'pop':
            cands = sorted([h for h in rest if h['popularity']], key=lambda h: h['popularity'])
            if len(cands) < 3:
                continue
            partners = cands[:3]
        else:  # mix: MC2位 + 人気上位2（軸・MC2位除く）
            p1 = rest[0]
            cands = sorted([h for h in rest[1:] if h['popularity']], key=lambda h: h['popularity'])
            if len(cands) < 2:
                continue
            partners = [p1] + cands[:2]

        if bet == 'uw':
            inv = 400
            ret = uw_payout(r['div'], ax['umaban'], [p['umaban'] for p in partners])
        elif bet == 'wide3':
            inv = 300
            ret = uw_payout(r['div'], ax['umaban'], [p['umaban'] for p in partners],
                            include_umaren=False)
        else:  # fuku
            inv = 100
            ret = fukusho_payout(r['div'], ax['umaban'])
        rows.append((inv, ret))
    return np.array(rows, dtype=float) if rows else np.zeros((0, 2))


GRID = list(product(('mc', 'pop', 'mix'), ('ダ', '芝', 'both'), (0.0, 0.5, 0.6),
                    ('uw', 'wide3', 'fuku')))
print(f'構成グリッド: {len(GRID)}通り\n')

by_year = {}
for r in races:
    by_year.setdefault(r['date'][:4], []).append(r)

wf_rows_all = []
fixedA_rows_all = []
oracle_total = [0.0, 0.0]
print(f'{"テスト年":>7} {"選定構成(学習ROI)":<42} {"OOS n":>6} {"OOS ROI":>8}')
for ty in TEST_YEARS:
    train = [r for y, rs in by_year.items() if y < ty for r in rs]
    test = by_year.get(ty, [])
    if not test:
        continue

    best = None
    for g in GRID:
        rows = eval_config(train, *g)
        if len(rows) < N_MIN_TRAIN:
            continue
        roi = rows[:, 1].sum() / rows[:, 0].sum() * 100
        if best is None or roi > best[1]:
            best = (g, roi, len(rows))
    g, troi, tn = best
    rows_t = eval_config(test, *g)
    oroi = rows_t[:, 1].sum() / rows_t[:, 0].sum() * 100 if len(rows_t) else 0
    wf_rows_all.append(rows_t)
    gname = f'相手{g[0]}/{g[1]}/MC>={g[2]}/{g[3]}'
    print(f'{ty:>7} {gname:<38}({troi:.1f}%) {len(rows_t):>6,} {oroi:>7.1f}%')

    # 固定構成A（現行AYO）
    rows_a = eval_config(test, 'mc', 'ダ', 0.0, 'uw')
    fixedA_rows_all.append(rows_a)

    # オラクル（テスト年内で最良 = in-sample上限）
    obest = None
    for g2 in GRID:
        rows2 = eval_config(test, *g2)
        if len(rows2) < 50:
            continue
        roi2 = rows2[:, 1].sum() / rows2[:, 0].sum() * 100
        if obest is None or roi2 > obest:
            obest = roi2
            orows = rows2
    oracle_total[0] += orows[:, 0].sum()
    oracle_total[1] += orows[:, 1].sum()


def agg_ci(rows_list, n_boot=5000):
    rows = np.concatenate([r for r in rows_list if len(r)])
    n = len(rows)
    roi = rows[:, 1].sum() / rows[:, 0].sum() * 100
    idx = rng.integers(0, n, size=(n_boot, n))
    rois = rows[idx, 1].sum(axis=1) / rows[idx, 0].sum(axis=1) * 100
    return roi, np.percentile(rois, 2.5), np.percentile(rois, 97.5), n


print()
roi, lo, hi, n = agg_ci(wf_rows_all)
print(f'WF-OOS 合算            : n={n:,}  ROI={roi:.1f}%  95%CI[{lo:.1f}%, {hi:.1f}%]')
roi, lo, hi, n = agg_ci(fixedA_rows_all)
print(f'固定構成A(現行AYO) OOS : n={n:,}  ROI={roi:.1f}%  95%CI[{lo:.1f}%, {hi:.1f}%]')
print(f'オラクル(事後選定,参考): ROI={oracle_total[1]/oracle_total[0]*100:.1f}%  '
      f'← WFとの差が「全期間最適化の水増し幅」の目安')
