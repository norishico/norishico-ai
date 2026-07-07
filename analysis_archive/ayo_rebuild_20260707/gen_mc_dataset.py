# -*- coding: utf-8 -*-
"""
AYO再構築用 leak-free MCデータセット生成（read-only: リポジトリのファイルは一切変更しない）

- MC: N_MC=1000（本番generate_mc_recordと同じ）、seed固定で再現可能
- last3f: 前走値（本番と同一ロジック。当該レース実測値は使わない = leak-free）
- gate: umaban→horse_numフォールバック
- 対象: 2020-01-01〜2026-06-14、芝+ダ、新馬・未勝利除外、4頭以上
- 配当(円/100円): 単勝・複勝1-3・馬連・ワイド1-3 をrace_idでjoin
- 出力: scratchpad/mc_dataset.pkl
"""
import os
import pickle
import sqlite3
import sys
import time

import numpy as np

sys.stdout.reconfigure(encoding='utf-8')
REPO = r'C:\Users\westr\norishiko_ai'
SCRATCH = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)  # keiba.db 相対参照のため（読み取りのみ）

from sim_bt_full import load_horse_hist, get_horse_history_fast  # noqa: E402
import generate_race_sim as gsim  # noqa: E402
from backtest_sim_lite import STYLE_DEF  # noqa: E402

SEED = 42
N_MC = 1000
BT_START = '2020-01-01'
BT_END = '2026-06-14'


def umaban_to_gate(umaban):
    if not umaban or umaban <= 0:
        return 4
    return min((umaban + 1) // 2, 8)


def run_mc_seeded(horses, race_info, n_mc, rng):
    """backtest_sim_lite.run_mc_lite と同ロジック（seed可能なrngを外から渡す版）"""
    n = len(horses)
    if n < 3:
        return [1.0 / n] * n

    top3_count = np.zeros(n)
    tc = race_info.get('track_cond', '良')
    venue = race_info.get('venue', '')
    distance = race_info.get('distance', 1600)

    COURSE_ADV = {'東京': {2600: -8}, '中山': {3390: 5, 3110: 4},
                  '阪神': {1800: 3}, '中京': {1800: 3, 2000: 3}}
    c_adj = COURSE_ADV.get(venue, {}).get(distance, 0)
    heavy = tc in ('重', '不良')
    heavy_v = 3.0 if tc == '不良' else 2.0

    n_nige = sum(1 for h in horses if h['style'] == '逃げ')
    n_front = sum(1 for h in horses if h['style'] in ('逃げ', '先行'))
    num_h = race_info.get('num_horses', n)
    pH, pM, pS = 0.25, 0.40, 0.35
    if n_nige >= 3:
        adj = min(0.20, 0.08 * (n_nige - 1))
        pH = min(0.55, pH + adj); pS = max(0.10, pS - adj * 0.7); pM = max(0.20, pM - adj * 0.3)
    elif n_nige == 2:
        pH = min(0.50, pH + 0.08); pS = max(0.15, pS - 0.05); pM = max(0.25, pM - 0.03)
    elif n_nige == 0:
        pH = max(0.05, pH - 0.10); pS = min(0.55, pS + 0.10)
    if n > 0 and n_front / n > 0.50:
        pH = min(0.50, pH + 0.05); pS = max(0.10, pS - 0.05)
    if num_h >= 17:
        pH = min(0.55, pH + 0.12); pS = max(0.10, pS - 0.09); pM = max(0.20, pM - 0.03)
    elif num_h >= 13:
        pH = min(0.50, pH + 0.08); pS = max(0.10, pS - 0.06); pM = max(0.20, pM - 0.02)
    elif num_h <= 8:
        pH = max(0.05, pH - 0.05); pS = min(0.55, pS + 0.04)
    inner_front = any(h.get('gate', 5) <= 4 and h['style'] in ('逃げ', '先行') for h in horses)
    outer_front = any(h.get('gate', 5) >= 5 and h['style'] in ('逃げ', '先行') for h in horses)
    if inner_front and outer_front:
        pH = min(0.55, pH + 0.03); pS = max(0.10, pS - 0.03)
    _t = pH + pM + pS
    pH, pM, pS = pH / _t, pM / _t, pS / _t

    for _ in range(n_mc):
        P = rng.choice(['H', 'M', 'S'], p=[pH, pM, pS])
        gain = np.zeros(n)
        for i, h in enumerate(horses):
            style = h['style']
            pace = STYLE_DEF.get(style, STYLE_DEF['先行'])
            v = pace.get(P, 60)
            l3f = h.get('last3f') or 34.5
            g = (75.0 * v / 100 - 70) * 0.45 + (34.0 - l3f) * 1.5
            bonus = 0.0
            if P == 'S':
                if style == '逃げ':
                    bonus = 2.0
                elif style == '先行':
                    bonus = 0.5
            if heavy:
                if style == '逃げ':
                    bonus += heavy_v
                elif style == '先行':
                    bonus += heavy_v * 0.6
                elif style in ('差し', '追い込み'):
                    bonus -= heavy_v * 0.5
            if c_adj > 0 and style in ('逃げ', '先行'):
                bonus += c_adj * 0.5
            elif c_adj < 0 and style in ('差し', '追い込み'):
                bonus += abs(c_adj) * 0.5
            gate = h.get('gate', 4)
            if gate >= 6 and style in ('逃げ', '先行'):
                if any(hh.get('gate', 4) <= 4 and hh['style'] in ('逃げ', '先行')
                       for j, hh in enumerate(horses) if j != i):
                    if P == 'H':
                        g -= 1.0
            gain[i] = g + bonus
        noise = rng.normal(0, 5, n)
        order = np.argsort(-(gain + noise))
        for pos in range(min(3, n)):
            top3_count[order[pos]] += 1

    return (top3_count / n_mc).tolist()


def main():
    t0 = time.time()
    rng = np.random.default_rng(SEED)

    conn = sqlite3.connect('file:keiba.db?mode=ro', uri=True)
    conn.execute('PRAGMA cache_size=-65536')
    conn.execute('PRAGMA temp_store=MEMORY')
    conn.execute('PRAGMA mmap_size=268435456')

    print('1/3 horse_hist一括ロード...', flush=True)
    horse_hist = load_horse_hist(conn)
    print(f'  {time.time()-t0:.1f}s  {len(horse_hist):,}組', flush=True)

    div_map = {}
    for row in conn.execute("""
        SELECT race_id, tansho_umaban, tansho_payout,
               fukusho1_umaban, fukusho1_payout, fukusho2_umaban, fukusho2_payout,
               fukusho3_umaban, fukusho3_payout,
               umaren_uma1, umaren_uma2, umaren_payout,
               wide1_uma1, wide1_uma2, wide1_payout,
               wide2_uma1, wide2_uma2, wide2_payout,
               wide3_uma1, wide3_uma2, wide3_payout
        FROM dividends
    """):
        div_map[row[0]] = row[1:]

    bt_races = conn.execute('''
        SELECT DISTINCT date, venue, race_num, race_id, surface, distance, track_cond
        FROM results
        WHERE date >= ? AND date <= ?
          AND surface IN ("芝","ダ")
          AND race_name NOT LIKE "%新馬%"
          AND race_name NOT LIKE "%未勝利%"
        ORDER BY date, venue, race_num
    ''', (BT_START, BT_END)).fetchall()
    print(f'2/3 MC計算 N_MC={N_MC} seed={SEED}  対象{len(bt_races)}R', flush=True)

    races_out = []
    t1 = time.time()
    for idx, r in enumerate(bt_races):
        if idx % 1000 == 0 and idx > 0:
            el = time.time() - t1
            eta = el / idx * (len(bt_races) - idx)
            print(f'  {idx}/{len(bt_races)}R  {el:.0f}s  残{eta:.0f}s', flush=True)

        date, venue, race_num, race_id, srf, dst, tc = (
            r[0], r[1], r[2], r[3], r[4], r[5], r[6] or '良')

        horses_raw = conn.execute('''
            SELECT horse_name, umaban, jockey, finish, num_horses, horse_num,
                   popularity, odds
            FROM results WHERE race_id=? ORDER BY horse_num
        ''', (race_id,)).fetchall()
        if len(horses_raw) < 4:
            continue

        horses = []
        for h in horses_raw:
            hn = (h[0] or '').strip()
            jk = (h[2] or '').strip()
            hist = get_horse_history_fast(hn, date, srf, horse_hist)
            style = gsim.classify_style(hist, dst or 1600, jockey=jk, surface=srf)
            prev_l3f = next((hh['last3f'] for hh in hist if hh.get('last3f')), None)
            uma = h[1] if h[1] is not None else h[5]
            horses.append({
                'horse_name': hn,
                'umaban': uma,
                'finish': h[3],
                'last3f': prev_l3f,
                'style': style,
                'gate': umaban_to_gate(uma),
                'popularity': h[6],
                'odds': h[7],
            })

        race_info = {'venue': venue, 'distance': dst or 1600,
                     'track_cond': tc, 'num_horses': len(horses)}
        try:
            rates = run_mc_seeded(horses, race_info, N_MC, rng)
        except Exception:
            continue
        for h, rate in zip(horses, rates):
            h['mc'] = round(float(rate), 4)
            del h['gate']

        races_out.append({
            'date': date, 'venue': venue, 'race_num': race_num, 'race_id': race_id,
            'surface': srf, 'distance': dst, 'track_cond': tc,
            'horses': horses,
            'div': div_map.get(race_id),
        })

    conn.close()
    out_path = os.path.join(SCRATCH, 'mc_dataset.pkl')
    with open(out_path, 'wb') as f:
        pickle.dump({'n_mc': N_MC, 'seed': SEED, 'bt_start': BT_START, 'bt_end': BT_END,
                     'races': races_out}, f)
    n_h = sum(len(r['horses']) for r in races_out)
    print(f'3/3 保存完了: {out_path}')
    print(f'  レース={len(races_out):,}  馬={n_h:,}  総所要={time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
