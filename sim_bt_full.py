"""
sim_bt_full.py — 全馬MC複勝率バックテスト（高速一括ロード版）

backtest_sim_lite.py からの改良:
  1. 全results+race_lapsを一括ロード (TRIM問題・SQLボトルネック解消)
  2. 各レースの全馬のMC複勝率を記録 (旧: 最高1頭のみ)
  3. 芝+ダート対応 (旧: 芝のみ)

出力: sim_bt_results_full.json
"""
import sqlite3, json, sys, time
import numpy as np
from collections import defaultdict
import generate_race_sim as gsim
from backtest_sim_lite import run_mc_lite

sys.stdout.reconfigure(encoding='utf-8')

N_MC = 20
BT_START = '2020-01-01'
BT_END   = '2026-06-14'
HIST_START = '2018-01-01'
SURFACES = ('芝', 'ダ')
OUTPUT_PATH = 'sim_bt_results_full.json'


def load_horse_hist(conn):
    """全results+race_lapsを一括ロード → (horse_name, surface) → 履歴dictリスト(日付昇順)"""
    rows = conn.execute('''
        SELECT r.horse_name, r.date, r.venue, r.surface, r.distance, r.finish,
               r.num_horses, r.pos4, r.last3f, r.time_sec, r.track_cond, r.race_id, r.jockey,
               rl.pace_type, rl.first_3f, rl.last_3f_race, r.race_name
        FROM results r
        LEFT JOIN race_laps rl ON rl.race_id = r.race_id
        WHERE r.date >= ? AND r.finish < 90
          AND r.surface IN ("芝","ダ")
        ORDER BY r.horse_name, r.date
    ''', (HIST_START,)).fetchall()

    horse_hist = defaultdict(list)
    for r in rows:
        entry = {
            'date': r[1], 'venue': r[2], 'surface': r[3], 'distance': r[4],
            'finish': r[5], 'num_horses': r[6], 'pos4': r[7], 'last3f': r[8],
            'time_sec': r[9], 'track_cond': r[10], 'race_id': r[11],
            'pace_type': r[13], 'first_3f': r[14], 'last_3f_race': r[15],
            'race_name': r[16] if r[16] is not None else '',
        }
        horse_hist[(r[0], r[3])].append(entry)

    # 各リストは既にORDER BY dateで昇順
    return dict(horse_hist)


def get_horse_history_fast(horse_name, current_date, surface, horse_hist):
    """SQLクエリなし版 get_horse_history (最大10件、日付降順)"""
    key = (horse_name, surface)
    all_for_surf = horse_hist.get(key, [])
    hist = [h for h in all_for_surf if h['date'] < current_date]
    hist = hist[-10:]  # 最新10件（昇順スライスなので末尾が最新）

    if len(hist) < 5:
        # fallback: 芝+ダート両方から合算
        combined = []
        for srf in SURFACES:
            for h in horse_hist.get((horse_name, srf), []):
                if h['date'] < current_date:
                    combined.append(h)
        combined.sort(key=lambda x: x['date'], reverse=True)
        fallback = combined[:10]
        if len(fallback) > len(hist):
            hist = fallback

    # classify_styleが日付降順を期待している場合があるのでreverseして渡す
    return list(reversed(hist))  # 最新が先頭


def umaban_to_gate(umaban):
    if not umaban or umaban <= 0:
        return 4
    return min((umaban + 1) // 2, 8)


def run_bt(conn, horse_hist):
    bt_races = conn.execute('''
        SELECT DISTINCT date, venue, race_num, race_id, surface, distance, track_cond
        FROM results
        WHERE date >= ? AND date <= ?
          AND surface IN ("芝","ダ")
          AND race_name NOT LIKE "%新馬%"
          AND race_name NOT LIKE "%未勝利%"
        ORDER BY date, venue, race_num
    ''', (BT_START, BT_END)).fetchall()

    print(f'BT対象: {len(bt_races)}R', flush=True)

    all_records = []
    t0 = time.time()

    for idx, r in enumerate(bt_races):
        if idx % 500 == 0 and idx > 0:
            elapsed = time.time() - t0
            eta = elapsed / idx * (len(bt_races) - idx)
            print(f'  {idx}/{len(bt_races)}R  {elapsed:.0f}s経過  残{eta:.0f}s', flush=True)

        date, venue, race_num, race_id, srf, dst, tc = (
            r[0], r[1], r[2], r[3], r[4], r[5], r[6] or '良'
        )

        horses_raw = conn.execute('''
            SELECT horse_name, umaban, jockey, finish, last3f, num_horses
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
            horses.append({
                'horse_name': hn,
                'finish': h[3],
                'last3f': h[4],
                'style': style,
                'gate': umaban_to_gate(h[1]),
            })

        race_info = {
            'venue': venue, 'distance': dst or 1600,
            'track_cond': tc, 'num_horses': len(horses),
        }

        try:
            top3_rates = run_mc_lite(horses, race_info, n_mc=N_MC)
        except Exception:
            continue

        for h, rate in zip(horses, top3_rates):
            fin = h['finish']
            all_records.append({
                'date': date, 'venue': venue, 'race_num': race_num,
                'horse_name': h['horse_name'], 'surface': srf,
                'mc_top3_rate': round(float(rate), 3),
                'style': h['style'],
                'actual_finish': fin,
                'hit': fin is not None and float(fin) <= 3.0,
            })

    elapsed = time.time() - t0
    print(f'MC計算完了: {elapsed:.1f}s  {len(all_records):,}件', flush=True)
    return all_records


def print_stats(records):
    n_total = len(records)
    n_races = len({(r['date'], r['venue'], r['race_num']) for r in records})
    print(f'\n=== sim_bt_full N_MC={N_MC} ===')
    print(f'総レース: {n_races}R  総記録: {n_total:,}件')

    for srf in SURFACES:
        sub = [r for r in records if r['surface'] == srf]
        print(f'  {srf}: {len(sub):,}件')

    print()
    print('MC閾値別 (3着内率・週件数):')
    for thr in [0.40, 0.50, 0.55, 0.60, 0.65]:
        sub = [r for r in records if r['mc_top3_rate'] >= thr]
        if not sub:
            continue
        hit_r = sum(1 for r in sub if r['hit']) / len(sub) * 100
        wkly = len(sub) / 208
        print(f'  MC>={thr:.0%}: n={len(sub):5d}  3着内率={hit_r:.1f}%  週{wkly:.1f}件')

    print()
    print('年別 MC>=0.50:')
    for yr in ['2022', '2023', '2024', '2025']:
        sub = [r for r in records if r['mc_top3_rate'] >= 0.50 and r['date'].startswith(yr)]
        if sub:
            hr = sum(1 for r in sub if r['hit']) / len(sub) * 100
            print(f'  {yr}: {len(sub):4d}件  3着内率={hr:.1f}%')

    print()
    print('脚質別 MC>=0.50:')
    by_style = defaultdict(list)
    for r in records:
        if r['mc_top3_rate'] >= 0.50:
            by_style[r['style']].append(r)
    for st, recs in sorted(by_style.items(), key=lambda x: -len(x[1])):
        hr = sum(1 for r in recs if r['hit']) / len(recs) * 100
        print(f'  {st}: {len(recs):4d}件  3着内率={hr:.1f}%')

    # MC>=0.50 × 差し+中団
    sub_target = [r for r in records if r['mc_top3_rate'] >= 0.50 and r['style'] in ('差し', '中団')]
    if sub_target:
        hr = sum(1 for r in sub_target if r['hit']) / len(sub_target) * 100
        wkly = len(sub_target) / 208
        print(f'\n[既存三連複条件: 差し+中団×MC>=0.50]')
        print(f'  {len(sub_target)}件  3着内率={hr:.1f}%  週{wkly:.1f}件')
        for yr in ['2022','2023','2024','2025']:
            ys = [r for r in sub_target if r['date'].startswith(yr)]
            if ys:
                hr2 = sum(1 for r in ys if r['hit']) / len(ys) * 100
                print(f'    {yr}: {len(ys)}件  {hr2:.1f}%')


def main():
    t_total = time.time()
    conn = sqlite3.connect('keiba.db')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA cache_size=-65536')
    conn.execute('PRAGMA temp_store=MEMORY')
    conn.execute('PRAGMA mmap_size=268435456')

    print('1/3 一括ロード中...', end='', flush=True)
    t0 = time.time()
    horse_hist = load_horse_hist(conn)
    print(f' {time.time()-t0:.1f}s  {len(horse_hist):,}組(馬×面種)', flush=True)

    print('2/3 MC計算中...', flush=True)
    records = run_bt(conn, horse_hist)
    conn.close()

    print('3/3 集計・保存中...', flush=True)
    print_stats(records)

    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump({
            'n_mc': N_MC,
            'surfaces': list(SURFACES),
            'all_horses': True,
            'records': records,
        }, f, ensure_ascii=False, default=str)
    print(f'\n-> {OUTPUT_PATH} 保存完了')
    print(f'総所要時間: {time.time()-t_total:.1f}s')


if __name__ == '__main__':
    main()
