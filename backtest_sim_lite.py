"""
backtest_sim_lite.py
軽量版シミュレーターBT: classify_style + MC20回
calc_horse_ratings()は使わず、脚質×ペース補正のみ
"""
import sqlite3, json, sys, time
import numpy as np
from collections import defaultdict
import generate_race_sim as gsim

SURFACE = '芝'
N_MC = 20
YEARS = [2022, 2023, 2024, 2025]

STYLE_DEF = gsim.STYLE_DEFAULTS  # H/M/S別デフォルト pace


def get_style_for_horse(conn, horse_name, date, surface, distance, jockey, hist_cache):
    """classify_styleをキャッシュ付きで呼ぶ（horse_name+surfaceキー）"""
    key = (horse_name, surface)
    if key not in hist_cache:
        hist = gsim.get_horse_history(conn, horse_name, date, surface)
        hist_cache[key] = hist
    history = hist_cache[key]
    style = gsim.classify_style(history, distance, jockey=jockey, surface=surface)
    return style, history


def run_mc_lite(horses, race_info, n_mc=N_MC):
    """軽量MC: sprint=75固定、脚質×ペース補正のみ"""
    n = len(horses)
    if n < 3:
        return [1.0 / n] * n

    top3_count = np.zeros(n)
    tc = race_info.get('track_cond', '良')
    venue = race_info.get('venue', '')
    distance = race_info.get('distance', 1600)

    COURSE_ADV = {
        '東京': {2600: -8}, '中山': {3390: 5, 3110: 4},
        '阪神': {1800: 3}, '中京': {1800: 3, 2000: 3}
    }
    c_adj = COURSE_ADV.get(venue, {}).get(distance, 0)
    heavy = tc in ('重', '不良')
    heavy_v = 3.0 if tc == '不良' else 2.0
    rng = np.random.default_rng()

    # ── 動的ペース確率 ───────────────────────────────────────────
    n_nige = sum(1 for h in horses if h['style'] == '逃げ')
    n_front = sum(1 for h in horses if h['style'] in ('逃げ', '先行'))
    num_horses = race_info.get('num_horses', n)

    pH, pM, pS = 0.25, 0.40, 0.35  # ベース確率

    # 逃げ馬数補正
    if n_nige >= 3:
        adj = min(0.20, 0.08 * (n_nige - 1))
        pH = min(0.55, pH + adj)
        pS = max(0.10, pS - adj * 0.7)
        pM = max(0.20, pM - adj * 0.3)
    elif n_nige == 2:
        pH = min(0.50, pH + 0.08)
        pS = max(0.15, pS - 0.05)
        pM = max(0.25, pM - 0.03)
    elif n_nige == 0:
        pH = max(0.05, pH - 0.10)
        pS = min(0.55, pS + 0.10)

    # 先行馬比率 > 50%
    if n > 0 and n_front / n > 0.50:
        pH = min(0.50, pH + 0.05)
        pS = max(0.10, pS - 0.05)

    # 頭数補正
    if num_horses >= 17:
        pH = min(0.55, pH + 0.12)
        pS = max(0.10, pS - 0.09)
        pM = max(0.20, pM - 0.03)
    elif num_horses >= 13:
        pH = min(0.50, pH + 0.08)
        pS = max(0.10, pS - 0.06)
        pM = max(0.20, pM - 0.02)
    elif num_horses <= 8:
        pH = max(0.05, pH - 0.05)
        pS = min(0.55, pS + 0.04)

    # 内外先行競合補正
    inner_front = any(h.get('gate', 5) <= 4 and h['style'] in ('逃げ', '先行') for h in horses)
    outer_front = any(h.get('gate', 5) >= 5 and h['style'] in ('逃げ', '先行') for h in horses)
    if inner_front and outer_front:
        pH = min(0.55, pH + 0.03)
        pS = max(0.10, pS - 0.03)

    # 正規化
    _pt = pH + pM + pS
    pH, pM, pS = pH / _pt, pM / _pt, pS / _pt

    for _ in range(n_mc):
        P = rng.choice(['H', 'M', 'S'], p=[pH, pM, pS])
        gain = np.zeros(n)

        for i, h in enumerate(horses):
            style = h['style']
            pace = STYLE_DEF.get(style, STYLE_DEF['先行'])
            v = pace.get(P, 60)
            burst = 75.0 * v / 100  # sprint固定75
            l3f = h.get('last3f') or 34.5
            g = (burst - 70) * 0.45 + (34.0 - l3f) * 1.5

            bonus = 0.0
            if P == 'S':
                if style == '逃げ': bonus = 2.0
                elif style == '先行': bonus = 0.5

            if heavy:
                if style == '逃げ': bonus += heavy_v
                elif style == '先行': bonus += heavy_v * 0.6
                elif style in ('差し', '追い込み'): bonus -= heavy_v * 0.5

            if c_adj > 0 and style in ('逃げ', '先行'):
                bonus += c_adj * 0.5
            elif c_adj < 0 and style in ('差し', '追い込み'):
                bonus += abs(c_adj) * 0.5

            gate = h.get('gate', 4)
            if gate >= 6 and style in ('逃げ', '先行'):
                inner = any(
                    hh.get('gate', 4) <= 4 and hh['style'] in ('逃げ', '先行')
                    for j, hh in enumerate(horses) if j != i
                )
                if inner and P == 'H':
                    g -= 1.0

            gain[i] = g + bonus

        noise = rng.normal(0, 5, n)
        order = np.argsort(-(gain + noise))
        for pos in range(min(3, n)):
            top3_count[order[pos]] += 1

    return (top3_count / n_mc).tolist()


def _umaban_to_gate(umaban):
    if not umaban or umaban <= 0:
        return 4
    import math
    return min(math.ceil(umaban / 2), 8)


def run_year_lite(year, conn):
    races = conn.execute("""
        SELECT DISTINCT date, venue, race_num, race_id, surface, distance, track_cond
        FROM results
        WHERE date >= ? AND date <= ?
          AND surface = ?
          AND race_name NOT LIKE '%新馬%'
          AND race_name NOT LIKE '%未勝利%'
        ORDER BY date, venue, race_num
    """, (f'{year}-01-01', f'{year}-12-31', SURFACE)).fetchall()

    records = []
    hist_cache = {}

    for r in races:
        race_id = r['race_id']
        race_info = {
            'date': r['date'], 'venue': r['venue'],
            'distance': r['distance'], 'track_cond': r['track_cond'] or '良',
            'num_horses': 0,  # 後でhorse構築後に更新
        }

        horses_raw = conn.execute("""
            SELECT horse_name, umaban, jockey, finish, last3f, num_horses
            FROM results WHERE race_id=? ORDER BY horse_num
        """, (race_id,)).fetchall()

        if len(horses_raw) < 4:
            continue

        horses = []
        for h in horses_raw:
            hn = h['horse_name'].strip() if h['horse_name'] else ''
            jk = h['jockey'].strip() if h['jockey'] else ''
            style, _ = get_style_for_horse(
                conn, hn, r['date'], r['surface'], r['distance'], jk, hist_cache
            )
            horses.append({
                'horse_name': hn,
                'finish': h['finish'],
                'last3f': h['last3f'],
                'style': style,
                'gate': _umaban_to_gate(h['umaban']),
            })

        race_info['num_horses'] = len(horses)
        top3_rates = run_mc_lite(horses, race_info, N_MC)
        best_idx = int(np.argmax(top3_rates))
        best = horses[best_idx]
        best_rate = top3_rates[best_idx]
        finish = best.get('finish')
        hit = finish is not None and float(finish) <= 3.0

        records.append({
            'date': r['date'], 'venue': r['venue'], 'race_num': r['race_num'],
            'mc_top3_rate': round(best_rate, 3),
            'horse_name': best['horse_name'],
            'style': best['style'],
            'actual_finish': finish,
            'hit': hit,
            'num_horses': race_info['num_horses'],
        })

    print(f"  {year}: {len(records)}R", flush=True)
    return records


if __name__ == '__main__':
    conn = sqlite3.connect('keiba.db')
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    all_records = []
    t0 = time.time()
    for yr in YEARS:
        ty = time.time()
        recs = run_year_lite(yr, conn)
        elapsed = time.time() - ty
        all_records.extend(recs)
        rem = len(YEARS) - YEARS.index(yr) - 1
        print(f"    -> {elapsed:.0f}s, 残{rem}年", flush=True)

    conn.close()
    total = time.time() - t0

    n_total = len(all_records)
    baseline = sum(1 for r in all_records if r['hit']) / n_total * 100

    print(f"\n=== シミュレーターBT 軽量版 N_MC={N_MC} ===")
    print(f"総レース数: {n_total}  ベースライン: {baseline:.1f}%  ({total:.0f}s)")

    for thr in [0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
        sub = [r for r in all_records if r['mc_top3_rate'] >= thr]
        if not sub: continue
        hit_r = sum(1 for r in sub if r['hit']) / len(sub) * 100
        wkly = len(sub) / 208
        print(f"MC>={thr:.0%}: n={len(sub):4d}  3着内率={hit_r:.1f}%  週{wkly:.1f}件")

    print("\n年別安定性 (MC>=0.50):")
    for yr in YEARS:
        sub = [r for r in all_records if r['mc_top3_rate'] >= 0.50 and r['date'].startswith(str(yr))]
        if sub:
            hr = sum(1 for r in sub if r['hit']) / len(sub) * 100
            print(f"  {yr}: {len(sub):3d}R  {hr:.1f}%")

    print("\n脚質別 (MC>=0.50):")
    by_style = defaultdict(list)
    for r in all_records:
        if r['mc_top3_rate'] >= 0.50:
            by_style[r['style']].append(r)
    for st, recs in sorted(by_style.items(), key=lambda x: -len(x[1])):
        hr = sum(1 for r in recs if r['hit']) / len(recs) * 100
        print(f"  {st}: {len(recs)}R  {hr:.1f}%")

    print("\n頭数別 (MC>=0.50):")
    by_heads = {'≤8': [], '9-12': [], '13-16': [], '17+': []}
    for r in all_records:
        if r['mc_top3_rate'] >= 0.50:
            nh = r.get('num_horses', 12)
            if nh <= 8:    by_heads['≤8'].append(r)
            elif nh <= 12: by_heads['9-12'].append(r)
            elif nh <= 16: by_heads['13-16'].append(r)
            else:          by_heads['17+'].append(r)
    for band, recs in by_heads.items():
        if recs:
            hr = sum(1 for r in recs if r['hit']) / len(recs) * 100
            print(f"  {band}頭: {len(recs)}R  {hr:.1f}%")

    # 前回結果との比較
    prev_path = 'sim_bt_results_lite.json'
    try:
        with open(prev_path, encoding='utf-8') as f:
            prev = json.load(f)
        prev_recs = prev.get('records', [])
        prev_sub = [r for r in prev_recs if r.get('mc_top3_rate', 0) >= 0.50]
        prev_hr = sum(1 for r in prev_sub if r.get('hit')) / len(prev_sub) * 100 if prev_sub else 0
        new_sub = [r for r in all_records if r['mc_top3_rate'] >= 0.50]
        new_hr = sum(1 for r in new_sub if r['hit']) / len(new_sub) * 100 if new_sub else 0
        print(f"\n前回比較 (MC>=0.50): {prev_hr:.1f}% ({len(prev_sub)}件) → {new_hr:.1f}% ({len(new_sub)}件)  差分={new_hr-prev_hr:+.1f}pt")
    except Exception:
        pass

    with open('sim_bt_results_lite_v2.json', 'w', encoding='utf-8') as f:
        json.dump({'n_mc': N_MC, 'lite': True, 'dynamic_pace': True, 'records': all_records},
                  f, ensure_ascii=False, default=str)
    print("\n-> sim_bt_results_lite_v2.json 保存完了")
