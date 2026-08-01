"""区間ラップタイム取得スクリプト

netkeibaのレース結果ページからハロンタイムを取得してrace_lapsテーブルに保存。
requestsベース（Selenium不要）で高速取得。

Usage:
  python fetch_laps.py                    # Phase 1: 2024-2026
  python fetch_laps.py --from 2019        # 指定年から
  python fetch_laps.py --year 2025        # 特定年のみ
  python fetch_laps.py --test 10          # テスト: 10件だけ
"""

import sys, os, time, json, re, sqlite3, argparse
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

import requests
from bs4 import BeautifulSoup

DB_PATH = Path(__file__).parent / 'keiba.db'

VENUE_CODES = {
    '札幌': '01', '函館': '02', '福島': '03', '新潟': '04',
    '東京': '05', '中山': '06', '中京': '07', '京都': '08',
    '阪神': '09', '小倉': '10',
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

# レート制限: 1.5秒間隔（BAN回避）
SLEEP_SEC = 3.0
BATCH_SIZE = 50  # DB書き込みバッチサイズ


def make_netkeiba_id(venue, kai, week_num, race_num):
    """DB情報からnetkeiba race_idを生成"""
    vc = VENUE_CODES.get(venue, '00')
    return f'{2000 + int(str(kai)[:4]) if kai > 100 else 2026}{vc}{kai:02d}{week_num:02d}{race_num:02d}'


def parse_lap_times(html):
    """HTMLからハロンタイムをパース"""
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find(class_='Race_HaronTime')
    if not table:
        return None, None

    rows = table.find_all('tr')
    if len(rows) < 3:
        return None, None

    # Row 0: headers (200m, 400m, ...)
    # Row 1: cumulative times
    # Row 2: section (lap) times
    header_cells = [c.text.strip() for c in rows[0].find_all(['th', 'td'])]
    cum_cells = [c.text.strip() for c in rows[1].find_all(['th', 'td'])]
    lap_cells = [c.text.strip() for c in rows[2].find_all(['th', 'td'])]

    # Parse lap times (区間タイム)
    laps = []
    for val in lap_cells:
        try:
            laps.append(float(val))
        except (ValueError, TypeError):
            continue

    # Parse cumulative times
    cums = []
    for val in cum_cells:
        val = val.strip()
        try:
            if ':' in val:
                parts = val.split(':')
                cums.append(float(parts[0]) * 60 + float(parts[1]))
            else:
                cums.append(float(val))
        except (ValueError, TypeError):
            continue

    if not laps:
        return None, None

    return laps, cums


def _time_for_distance(laps, seg_lens, target_dist):
    """先頭からtarget_dist(m)ぶんの累積時間(区間をまたぐ場合は按分)。距離が足りなければNone。"""
    acc_dist, acc_time = 0.0, 0.0
    for lap, seg_len in zip(laps, seg_lens):
        if acc_dist + seg_len <= target_dist + 1e-9:
            acc_time += lap
            acc_dist += seg_len
        else:
            remaining = target_dist - acc_dist
            frac = remaining / seg_len if seg_len > 0 else 0.0
            acc_time += lap * frac
            acc_dist = target_dist
            break
    return acc_time if acc_dist >= target_dist - 1e-6 else None


def calc_derived(laps, distance):
    """ラップタイムから派生指標を計算

    【2026-08-01発見・修正】netkeibaのハロンタイム表は距離が200mの倍数でない場合、
    最初の区間だけ短い端数(distance%200 m)になる(実データで150m/100mの端数を複数会場
    ×距離で確認済み: 端数150m→約9.3秒、端数100m→約7.0-7.2秒、通常200m区間は11-13秒)。
    これを無視して`laps[:3]`等を単純平均すると、端数区間の短い絶対秒数に引きずられて
    前半が異常に速く見え、pace_typeがほぼ確実に'H'に誤判定される(該当11会場×距離組み
    合わせで実際にH率95-100%という異常値を確認、修正前バグ)。

    修正はdistance%200!=0の場合のみ物理距離ベースの按分計算に切り替え、distance%200==0
    (全区間200mで均一、大多数のレース)は従来のcount-basedロジックをそのまま維持する
    (奇数区間数nの場合、count-based平均は区間数の違いを自動的に正規化できておりバグは
    無い。物理距離ベースに一律置き換えるとn奇数レースの分割点がずれ、無関係な既存の
    挙動まで変えてしまうため、影響範囲をバグの実在する端数区間ケースだけに限定する)。
    """
    n = len(laps)
    if n < 3:
        return {}

    remainder = distance % 200 if distance else 0

    if remainder == 0:
        # 従来ロジック(全区間200mで均一、変更なし)
        first_3f = sum(laps[:3])
        last_3f = sum(laps[-3:])
        if n > 6:
            mid = sum(laps[3:-3])
        elif n > 3:
            mid = sum(laps[3:])
        else:
            mid = None
        half = n // 2
        front_avg = sum(laps[:half]) / half
        back_avg = sum(laps[half:]) / (n - half)
    else:
        # 端数区間ケース: 物理距離(m)ベースで按分計算
        seg_lens = [float(remainder)] + [200.0] * (n - 1)
        total_len = sum(seg_lens)
        first_3f = _time_for_distance(laps, seg_lens, 600.0)
        last_3f = _time_for_distance(list(reversed(laps)), list(reversed(seg_lens)), 600.0)
        if first_3f is not None and last_3f is not None and total_len > 1200:
            mid = sum(laps) - first_3f - last_3f
        else:
            mid = None
        half_dist = total_len / 2
        front_time = _time_for_distance(laps, seg_lens, half_dist)
        back_time = (sum(laps) - front_time) if front_time is not None else None
        if front_time is not None and back_time is not None and (total_len - half_dist) > 0:
            front_avg = front_time / half_dist * 200   # 200m換算に揃えて従来閾値をそのまま使う
            back_avg = back_time / (total_len - half_dist) * 200
        else:
            front_avg = back_avg = None

    # ペース加速ポイント: 最も遅いハロンの次のハロン
    # (ペースが緩んだ後に加速開始。indexベースの探索で区間長には依存しないため両ケース共通)
    if n >= 4:
        # 後半のラップで最小値(最速)のindexを探す
        mid_start = max(2, n // 3)
        slowest_idx = mid_start
        slowest_val = laps[mid_start]
        for i in range(mid_start, n - 1):
            if laps[i] > slowest_val:
                slowest_val = laps[i]
                slowest_idx = i
        accel_point = slowest_idx + 1  # 1-indexed, 加速開始ハロン
    else:
        accel_point = None

    # ペースタイプ: 前半 vs 後半の比率(閾値0.3秒は従来通り、両ケース共通)
    if front_avg is None or back_avg is None:
        pace_type = None
    elif front_avg < back_avg - 0.3:
        pace_type = 'H'  # ハイ (前半速い)
    elif front_avg > back_avg + 0.3:
        pace_type = 'S'  # スロー (後半速い)
    else:
        pace_type = 'M'  # ミドル

    return {
        'first_3f': round(first_3f, 1) if first_3f else None,
        'last_3f_race': round(last_3f, 1) if last_3f else None,
        'mid_section': round(mid, 1) if mid else None,
        'accel_point': accel_point,
        'pace_type': pace_type,
        'n_furlongs': n,
    }


def fetch_and_store(conn, races, limit=None):
    """レース一覧を取得してDBに保存"""
    total = len(races)
    if limit:
        races = races[:limit]
        total = len(races)

    # 既存レースをスキップ
    existing = set(r[0] for r in conn.execute('SELECT race_id FROM race_laps').fetchall())
    races = [r for r in races if r['our_id'] not in existing]
    skipped = total - len(races) - (total - len(races) if not limit else 0)

    print(f'  対象: {len(races)}R (既存スキップ: {len(existing)}件)')
    if not races:
        print('  全て取得済み')
        return

    session = requests.Session()
    session.headers.update(HEADERS)

    batch = []
    t_start = time.time()
    success = 0
    errors = 0
    no_data = 0

    for i, race in enumerate(races):
        nk_id = race['nk_id']
        url = f'https://race.netkeiba.com/race/result.html?race_id={nk_id}'

        try:
            resp = session.get(url, timeout=15)
            if resp.status_code != 200:
                errors += 1
                continue

            laps, cums = parse_lap_times(resp.text)
            if not laps:
                no_data += 1
                # Still record as attempted (empty) to avoid re-fetching
                batch.append((
                    race['our_id'], race['date'], race['venue'], race['race_num'],
                    race['surface'], race['distance'],
                    None, None, None, None, None, None, None, 0
                ))
            else:
                derived = calc_derived(laps, race['distance'])
                batch.append((
                    race['our_id'], race['date'], race['venue'], race['race_num'],
                    race['surface'], race['distance'],
                    json.dumps(laps), json.dumps(cums) if cums else None,
                    derived.get('first_3f'), derived.get('last_3f_race'),
                    derived.get('mid_section'), derived.get('accel_point'),
                    derived.get('pace_type'), derived.get('n_furlongs'),
                ))
                success += 1

        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f'  ⚠ Error {nk_id}: {e}')

        # バッチ書き込み
        if len(batch) >= BATCH_SIZE:
            _write_batch(conn, batch)
            batch = []

        # 進捗表示 (100Rごと)
        done = i + 1
        if done % 100 == 0 or done == len(races):
            elapsed = time.time() - t_start
            rate = done / elapsed * 60 if elapsed > 0 else 0
            remaining = (len(races) - done) / (rate / 60) if rate > 0 else 0
            pct = done / len(races) * 100
            print(f'\r  [{done}/{len(races)}] {pct:.0f}% | {rate:.0f}R/min | '
                  f'成功{success} エラー{errors} データなし{no_data} | '
                  f'残り{remaining:.0f}秒', end='', flush=True)

        time.sleep(SLEEP_SEC)

    # 残りバッチ
    if batch:
        _write_batch(conn, batch)

    elapsed = time.time() - t_start
    print(f'\n  ✅ 完了: {success}R取得, {errors}エラー, {no_data}データなし ({elapsed:.0f}秒)')


def _write_batch(conn, batch):
    """バッチINSERT"""
    conn.executemany('''
        INSERT OR REPLACE INTO race_laps
        (race_id, date, venue, race_num, surface, distance,
         lap_times, cumulative, first_3f, last_3f_race,
         mid_section, accel_point, pace_type, n_furlongs)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', batch)
    conn.commit()


def get_race_list(conn, year_from, year_to):
    """取得対象レースリストを生成"""
    q = '''
        SELECT DISTINCT date, venue, race_num, kai, week_num, surface, distance
        FROM results
        WHERE date BETWEEN ? AND ?
          AND kai IS NOT NULL AND week_num IS NOT NULL
          AND (race_name IS NULL OR race_name NOT LIKE '%障害%')
        ORDER BY date, venue, race_num
    '''
    rows = conn.execute(q, (f'{year_from}-01-01', f'{year_to}-12-31')).fetchall()

    races = []
    for r in rows:
        date, venue, rnum, kai, wn, surface, dist = r
        year = int(date[:4])
        vc = VENUE_CODES.get(venue, '00')
        nk_id = f'{year}{vc}{kai:02d}{wn:02d}{rnum:02d}'
        our_id = f'{date}_{venue}_{rnum}'
        sf = '芝' if '芝' in str(surface) else 'ダ'
        races.append({
            'nk_id': nk_id, 'our_id': our_id,
            'date': date, 'venue': venue, 'race_num': rnum,
            'surface': sf, 'distance': dist or 0,
        })
    return races


def main():
    parser = argparse.ArgumentParser(description='区間ラップ取得')
    parser.add_argument('--from', type=int, dest='year_from', default=2024)
    parser.add_argument('--to', type=int, dest='year_to', default=2026)
    parser.add_argument('--year', type=int, help='特定年のみ')
    parser.add_argument('--test', type=int, help='テスト件数')
    args = parser.parse_args()

    if args.year:
        args.year_from = args.year
        args.year_to = args.year

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    print(f'🏇 区間ラップ取得 ({args.year_from}-{args.year_to}年)')
    races = get_race_list(conn, args.year_from, args.year_to)
    print(f'  対象レース: {len(races)}R')

    t0 = time.time()
    fetch_and_store(conn, races, limit=args.test)
    total_time = time.time() - t0

    stored = conn.execute('SELECT COUNT(*) FROM race_laps WHERE lap_times IS NOT NULL').fetchone()[0]
    print(f'\n  DB内ラップデータ: {stored}R')
    print(f'  合計時間: {total_time:.0f}秒')

    conn.close()


if __name__ == '__main__':
    main()
