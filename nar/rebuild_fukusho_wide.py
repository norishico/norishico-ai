"""rebuild_fukusho_wide.py - nar_dividends の複勝/ワイド payout 再構築

過去バグ: build_nar_db_fast.py の旧パーサーが複勝(3着分)・ワイド(3組)を
1行にしか保存できず、複勝は2/3の払戻金額が完全消失、ワイドはcombinationが
ハイフンなし連結(2025年以降は行数自体も欠損)していた。
パーサー自体は修正済み(MULTI_SINGLE/MULTI_PAIR, build_nar_db_fast.py)。
本スクリプトは対象race_idを再取得し、fukusho/wideの既存行をDELETE→
正しい3行をINSERTし直す。

使い方:
  python nar/rebuild_fukusho_wide.py --dry-run          # 対象件数のみ確認
  python nar/rebuild_fukusho_wide.py --db path/to/test.db --limit 20   # 検証用
  python nar/rebuild_fukusho_wide.py                    # 本番全件実行(未使用・呼び出し禁止)
"""
import sys, os, time, sqlite3, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_nar_db_fast import fetch_race_data

PROJ = Path(__file__).parent.parent
DB_PATH = PROJ / 'nar_keiba.db'
SLEEP = 0.5


def get_target_race_ids(conn, limit=None):
    """fukusho/wideのいずれかが3行未満のrace_idを返す"""
    q = """
    SELECT race_id FROM (
        SELECT race_id, bet_type, COUNT(*) c FROM nar_dividends
        WHERE bet_type IN ('fukusho','wide')
        GROUP BY race_id, bet_type
    )
    WHERE c < 3
    GROUP BY race_id
    ORDER BY race_id
    """
    if limit:
        q += f" LIMIT {limit}"
    return [r[0] for r in conn.execute(q).fetchall()]


def repair_one_race(conn, race_id, verbose=False):
    """1レース分を再取得し、fukusho/wideをDELETE→INSERTし直す。
    戻り値: 'ok' / 'fetch_failed' / 'incomplete'(3行そろわなかった)
    """
    data = fetch_race_data(race_id)
    if data is None:
        return 'fetch_failed'

    fuku = [d for d in data['dividends'] if d['bet_type'] == 'fukusho']
    wide = [d for d in data['dividends'] if d['bet_type'] == 'wide']

    if len(fuku) != 3 or len(wide) != 3:
        if verbose:
            print(f"  [INCOMPLETE] {race_id}: fukusho={len(fuku)} wide={len(wide)}")
        return 'incomplete'

    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM nar_dividends WHERE race_id=? AND bet_type IN ('fukusho','wide')", (race_id,))
        conn.executemany("""
            INSERT INTO nar_dividends (race_id,bet_type,combination,payout,pop_rank)
            VALUES(:race_id,:bet_type,:combination,:payout,:pop_rank)
        """, fuku + wide)
        conn.commit()
        return 'ok'
    except Exception as e:
        conn.rollback()
        if verbose:
            print(f"  [DB ERROR] {race_id}: {e}")
        return 'db_error'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=str(DB_PATH), help='対象DB(検証時はコピーを指定)')
    ap.add_argument('--limit', type=int, default=None, help='対象race_id数を制限(検証用)')
    ap.add_argument('--dry-run', action='store_true', help='対象件数のみ表示して終了')
    ap.add_argument('--workers', type=int, default=1, help='安全のためデフォルト直列')
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL")

    targets = get_target_race_ids(conn, args.limit)
    print(f"対象race_id: {len(targets)}件")

    if args.dry_run:
        conn.close()
        return

    ok = incomplete = failed = db_error = 0
    for i, rid in enumerate(targets):
        time.sleep(SLEEP)
        status = repair_one_race(conn, rid, verbose=True)
        if status == 'ok':
            ok += 1
        elif status == 'incomplete':
            incomplete += 1
        elif status == 'fetch_failed':
            failed += 1
        else:
            db_error += 1
        if (i + 1) % 50 == 0:
            print(f"  [進捗] {i+1}/{len(targets)} ok={ok} incomplete={incomplete} failed={failed} db_error={db_error}")

    print(f"\n完了: ok={ok} incomplete={incomplete} fetch_failed={failed} db_error={db_error}")
    conn.close()


if __name__ == '__main__':
    main()
