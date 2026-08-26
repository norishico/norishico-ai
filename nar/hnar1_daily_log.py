# -*- coding: utf-8 -*-
"""hnar1_daily_log.py - H-NAR-1(ペース免罪仮説) 日次観測ログ

⚠️ 実弾投入・馬券購入は一切行わない。2026-09からのプロスペクティブ観測(紙上検証)専用。
v3.3(nar/scoring_nar.py・nar/predict_nar.py・nar/nar_daily.py本体)には一切変更を
加えない、完全に独立した追加処理。

特徴量計算(trailing pace_z / 1角通過順)は nar/hnar1_pace_excuse.py の既存関数
(load_race_pace / load_corner1)をそのまま再利用する。リーク防止ロジックの
再実装によるバグ再混入を避けるため、ここでは絶対に再実装しない。

注意: hnar1_pace_excuse.build_dataset() はバックテスト専用(nar_results に
odds/finish が既に存在する = レース確定後のデータ)しか扱えない。当日の
未確定レース(まだ nar_results に行が無い)には使えないため、本ファイルでは
「対象日より前の直近過去走」を馬名(TRIM)で引き当てる専用ロジックを別途用意する。
これは特徴量そのものの再計算ではなく、どの過去レースを「前走」として参照するか
を選ぶだけの軽量な結合処理であり、pace_z/corner等の計算式自体は一切いじらない。

使い方:
  python nar/hnar1_daily_log.py                     # 本日分
  python nar/hnar1_daily_log.py --date 2026-08-27    # 指定日
"""
import sys
import json
import argparse
import subprocess
import sqlite3
import traceback
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import numpy as np

sys.stdout.reconfigure(encoding='utf-8')

PROJ = Path(__file__).parent.parent
NAR_DIR = Path(__file__).parent
sys.path.insert(0, str(NAR_DIR))

from hnar1_pace_excuse import (  # noqa: E402  既存関数をそのまま再利用
    load_race_pace, load_corner1,
    PACE_Z_THRESHOLD, PREV_C1_MAX, PREV_FINISH_MIN, ODDS_MIN, ODDS_MAX,
)

DB_URI = 'file:C:/Users/westr/norishiko_ai/nar_keiba.db?mode=ro'
LOG_PATH = PROJ / 'nar_predictions' / 'hnar1_observation_log.json'
SHUTSUBA_WORK_PATH = PROJ / 'nar_shutsuba_hnar1.json'  # H-NAR-1専用の作業ファイル(v3.3のnar_shutsuba.jsonとは別物)


def _fetch_shutsuba(target_str: str):
    """当日出馬表を取得する。既存の fetch_nar_shutsuba.py をそのまま再利用し、
    v3.3側の nar_shutsuba.json とは別の専用ファイルに書き出す(相互に干渉しない)。
    """
    script = NAR_DIR / 'fetch_nar_shutsuba.py'
    result = subprocess.run(
        [sys.executable, str(script), '--date', target_str, '--output', str(SHUTSUBA_WORK_PATH)],
        capture_output=True, text=True, encoding='utf-8', cwd=str(PROJ)
    )
    if result.returncode != 0:
        raise RuntimeError(f"fetch_nar_shutsuba失敗: {(result.stderr or '').strip()[:500]}")
    if not SHUTSUBA_WORK_PATH.exists():
        raise RuntimeError("出馬表ファイルが生成されなかった")
    with open(SHUTSUBA_WORK_PATH, encoding='utf-8') as f:
        races = json.load(f)
    return races


def _build_prev_race_features(conn, target_str: str) -> pd.DataFrame:
    """馬名(TRIM) -> 直近過去走(target_str より前で最新)の特徴量。

    pace_z・1角順位の算出は hnar1_pace_excuse.load_race_pace / load_corner1
    (既存関数)をそのまま利用する。ここでは「どの過去レースが前走か」を
    馬名で引き当てるだけ。
    """
    pace_map = load_race_pace(conn)
    c1_map = load_corner1(conn)

    res = pd.read_sql("""
        SELECT r.race_id, TRIM(r.horse_name) hn, r.umaban, r.finish, ra.date
        FROM nar_results r JOIN nar_races ra USING(race_id)
        WHERE r.umaban IS NOT NULL AND r.finish IS NOT NULL AND ra.date < ?
    """, conn, params=[target_str])

    res['c1_rank'] = [c1_map.get(rid, {}).get(u, np.nan) for rid, u in zip(res['race_id'], res['umaban'])]
    res['pace_z'] = res['race_id'].map(pace_map)
    res = res.sort_values(['hn', 'date'])
    last = res.groupby('hn', as_index=True).tail(1).set_index('hn')
    return last


def _append_log(record: dict):
    """観測ログに1日分のレコードを追記(蓄積)する。同日分の再実行時は
    その日のレコードのみ置き換える(重複防止。過去日は一切変更しない)。
    """
    LOG_PATH.parent.mkdir(exist_ok=True)
    log = []
    if LOG_PATH.exists():
        try:
            with open(LOG_PATH, encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                log = loaded
        except Exception:
            log = []
    log = [r for r in log if r.get('date') != record['date']]
    log.append(record)
    log.sort(key=lambda r: r.get('date', ''))
    with open(LOG_PATH, 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def run(target_str: str = None) -> dict:
    """H-NAR-1日次観測を実行し、結果を hnar1_observation_log.json に追記して返す。
    実弾投入・馬券購入は一切行わない(記録のみ)。
    どのような例外が起きてもここで完結させ、呼び出し元(v3.3側)には伝播させない。
    """
    target_str = target_str or date.today().isoformat()
    record = {
        'date': target_str,
        'run_at': datetime.now().isoformat(timespec='seconds'),
        'status': 'ok',
        'races_checked': 0,
        'horses_checked': 0,
        'picks': [],
        'note': '',
    }
    try:
        races = _fetch_shutsuba(target_str)
        record['races_checked'] = len(races)

        if not races:
            record['note'] = '本日該当なし(南関東レースなし)'
            _append_log(record)
            return record

        conn = sqlite3.connect(DB_URI, uri=True)
        conn.execute("PRAGMA query_only=1")
        conn.execute("PRAGMA cache_size=-65536")
        conn.execute("PRAGMA temp_store=MEMORY")
        try:
            prev = _build_prev_race_features(conn, target_str)
        finally:
            conn.close()

        horses_checked = 0
        picks = []
        for race in races:
            for e in race.get('entries', []):
                horses_checked += 1
                hn = (e.get('horse_name') or '').strip()
                if not hn or hn not in prev.index:
                    continue
                row = prev.loc[hn]
                prev_c1 = row['c1_rank']
                prev_pace_z = row['pace_z']
                prev_finish = row['finish']
                odds = e.get('odds')
                if pd.isna(prev_c1) or pd.isna(prev_pace_z) or pd.isna(prev_finish) or odds is None:
                    continue
                cond = (
                    (prev_pace_z >= PACE_Z_THRESHOLD)
                    and (prev_c1 <= PREV_C1_MAX)
                    and (prev_finish >= PREV_FINISH_MIN)
                    and (ODDS_MIN <= odds <= ODDS_MAX)
                )
                if not cond:
                    continue
                picks.append({
                    'race_id': race.get('race_id'),
                    'venue': race.get('venue_name'),
                    'race_num': race.get('race_num'),
                    'umaban': e.get('umaban'),
                    'horse_name': hn,
                    'odds': odds,
                    'jockey': e.get('jockey'),
                    'prev_race_id': row['race_id'],
                    'prev_date': row['date'],
                    'prev_pace_z': round(float(prev_pace_z), 3),
                    'prev_c1': int(prev_c1),
                    'prev_finish': int(prev_finish),
                })

        record['horses_checked'] = horses_checked
        record['picks'] = picks
        record['note'] = f'{len(picks)}頭該当' if picks else '本日該当なし(条件を満たす馬なし)'
    except Exception as e:
        record['status'] = 'error'
        record['note'] = f'{type(e).__name__}: {e}'
        record['traceback'] = traceback.format_exc()[-2000:]

    _append_log(record)
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', help='YYYY-MM-DD (default: today)')
    args = parser.parse_args()

    target_str = args.date or date.today().isoformat()
    print(f"=== H-NAR-1 daily observation {target_str} (実弾投入なし・記録のみ) ===")
    result = run(target_str)
    print(f"status={result['status']} races_checked={result['races_checked']} "
          f"horses_checked={result['horses_checked']} picks={len(result['picks'])}")
    print(f"note: {result['note']}")
    for p in result['picks']:
        print(f"  {p['venue']}{p['race_num']}R {p['umaban']}番 {p['horse_name']} "
              f"odds={p['odds']} 前走pace_z={p['prev_pace_z']} 前走1角={p['prev_c1']} 前走着順={p['prev_finish']}")
    print(f"ログ: {LOG_PATH}")
    return result


if __name__ == '__main__':
    main()
