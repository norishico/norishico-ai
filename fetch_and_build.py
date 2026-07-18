"""fetch_and_build.py - 自動データパイプライン (64bit Python)

JV-Link → TARGET CSV → staging DB → 件数チェック → ATOMIC swap

フロー:
  1. last_fetch.txt から前回取得時刻を読み、無ければ 7日前
  2. 32bit python で jvlink_fetch.py を実行 → jvlink_dump_results.csv / _dividends.csv
  3. staging保護: staging の MAX(date) > prod の MAX(date) なら cloneせず staging を継続使用
     (前回NGで昇格できなかったデータを保持するため)
  4. build_db.build_db(csvs, db_path="keiba_staging.db") で差分流し込み
  5. 件数チェック: staging results件数が prod 比 -10%以上減ったらNG (BTは実行しない)
  6. OK なら ATOMIC swap で本番昇格
  7. 失敗時は keiba.db を触らず alerts_log.py に記録

使い方:
  python fetch_and_build.py                     # 差分自動取得
  python fetch_and_build.py --from 20260401000000
  python fetch_and_build.py --skip-fetch        # 既存CSV使い検証のみ
  python fetch_and_build.py --dry-run           # swapしない
"""

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
PY32 = Path(r"C:\Users\westr\AppData\Local\Programs\Python\Python312-32\python.exe")

DB_PROD    = ROOT / "keiba.db"
DB_STAGING = ROOT / "keiba_staging.db"
LAST_FETCH = ROOT / "last_fetch.txt"
LAST_FETCH_PARALLEL = ROOT / "last_fetch_parallel.txt"
LOG_DIR    = ROOT / "logs"
DUMP_JSON  = ROOT / "jvlink_dump.json"
RESULTS_CSV   = ROOT / "jvlink_dump_results.csv"
DIVIDENDS_CSV = ROOT / "jvlink_dump_dividends.csv"

# 件数チェック: staging results が prod 比この割合以上減ったら構造破壊と判定
COUNT_DROP_LIMIT = 0.10  # 10%超減少でNG


def log(msg, fh=None):
    line = f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(line.encode("utf-8", "replace") + b"\n")
        sys.stdout.flush()
    if fh:
        fh.write(line + "\n")
        fh.flush()


def load_last_fetch(parallel=False):
    path = LAST_FETCH_PARALLEL if parallel else LAST_FETCH
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    # 初回は7日前
    d = dt.datetime.now() - dt.timedelta(days=7)
    return d.strftime("%Y%m%d000000")


def save_last_fetch(value, parallel=False):
    path = LAST_FETCH_PARALLEL if parallel else LAST_FETCH
    path.write_text(value, encoding="utf-8")


JVLINK_AGENT = Path(r"C:\Program Files (x86)\JRA-VAN\Data Lab\JVLinkAgent.exe")

def ensure_jvlink_agent(fh):
    """JVLinkAgentが起動していなければ自動起動して5秒待つ"""
    import subprocess as _sp
    result = _sp.run(["tasklist", "/FI", "IMAGENAME eq JVLinkAgent.exe"],
                      capture_output=True, text=True, encoding="cp932", errors="replace")
    if "JVLinkAgent.exe" not in (result.stdout or ""):
        if JVLINK_AGENT.exists():
            log("JVLinkAgent not running — starting...", fh)
            _sp.Popen([str(JVLINK_AGENT)])
            time.sleep(5)
            log("JVLinkAgent started", fh)
        else:
            log(f"WARNING: JVLinkAgent not found at {JVLINK_AGENT}", fh)


def run_fetch(fromtime, fh):
    if not PY32.exists():
        raise RuntimeError(f"32bit Python not found: {PY32}")
    ensure_jvlink_agent(fh)
    log(f"JV-Link fetch start fromtime={fromtime}", fh)
    t0 = time.time()
    proc = subprocess.run(
        [str(PY32), str(ROOT / "jvlink_fetch.py"), fromtime, str(DUMP_JSON)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="cp932",
        errors="replace",
    )
    if fh:
        fh.write(proc.stdout or "")
        if proc.stderr:
            fh.write("STDERR:\n" + proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"jvlink_fetch.py failed rc={proc.returncode}")
    log(f"JV-Link fetch done in {time.time()-t0:.0f}s", fh)
    if not RESULTS_CSV.exists() or not DIVIDENDS_CSV.exists():
        raise RuntimeError(f"CSV not produced: {RESULTS_CSV} / {DIVIDENDS_CSV}")


def _db_max_date(db_path):
    """DB の results MAX(date) を返す。存在しなければ None。"""
    try:
        c = sqlite3.connect(str(db_path))
        row = c.execute("SELECT MAX(date) FROM results").fetchone()
        c.close()
        return row[0] if row else None
    except Exception:
        return None


def _db_results_count(db_path):
    """DB の results 件数を返す。存在しなければ 0。"""
    try:
        c = sqlite3.connect(str(db_path))
        row = c.execute("SELECT COUNT(*) FROM results").fetchone()
        c.close()
        return row[0] if row else 0
    except Exception:
        return 0


def build_staging(fh):
    """keiba.db → keiba_staging.db 複製 → CSV 流し込み。
    staging が prod より新しいデータを持っている場合は clone せず継続使用。
    """
    if DB_PROD.exists():
        prod_max = _db_max_date(DB_PROD)
        stg_max  = _db_max_date(DB_STAGING) if DB_STAGING.exists() else None
        if stg_max and prod_max and stg_max > prod_max:
            log(f"staging保護: staging({stg_max}) > prod({prod_max}) → cloneスキップ、staging継続使用", fh)
        else:
            # conn.backup() でWAL安全コピー (shutil.copy2禁止・unlink不要)
            log(f"clone {DB_PROD.name} → {DB_STAGING.name}", fh)
            src_conn = sqlite3.connect(str(DB_PROD))
            try:
                src_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                dst_conn = sqlite3.connect(str(DB_STAGING))
                try:
                    src_conn.backup(dst_conn)
                    log("backup complete", fh)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()
    else:
        log("no prod DB; fresh staging", fh)
        if DB_STAGING.exists():
            DB_STAGING.unlink()

    # build_db を import して直接呼び出し (CLIは --db 未対応)
    sys.path.insert(0, str(ROOT))
    import build_db as bdb  # type: ignore
    log("build_db into staging", fh)
    bdb.build_db([str(RESULTS_CSV), str(DIVIDENDS_CSV)], db_path=str(DB_STAGING))


def run_jvlink_training_import(fh):
    """JV-Link 取得済み調教データ(jvlink_dump_training.json) を training テーブル投入
    jvlink_fetch.py が SLOP+WOOD dataspec で取得してJSON出力している前提
    """
    script = ROOT / "scripts" / "import_training_from_jvlink.py"
    if not script.exists():
        log("jvlink training import script missing, skip", fh)
        return
    log("jvlink training import start", fh)
    t0 = time.time()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(script)],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    if fh:
        fh.write(proc.stdout or "")
        if proc.stderr:
            fh.write("STDERR:\n" + proc.stderr)
    if proc.returncode != 0:
        log(f"[WARN] jvlink training import rc={proc.returncode}", fh)
    else:
        log(f"jvlink training import done in {time.time()-t0:.0f}s", fh)


def run_blood_fetch(fh):
    """netkeiba血統スクレイパを staging DB に対して実行。
    JV-Link蓄積系(UM)が契約プラン制約で取れないので代替経路。
    既知horse_idはblood_cache.jsonからスキップされるので2回目以降は数秒。
    """
    script = ROOT / "scripts" / "fetch_blood_netkeiba.py"
    if not script.exists():
        log("blood_fetch script missing, skip", fh)
        return
    log("blood fetch start (netkeiba scraper on staging)", fh)
    t0 = time.time()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(script)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if fh:
        fh.write(proc.stdout or "")
        if proc.stderr:
            fh.write("STDERR:\n" + proc.stderr)
    if proc.returncode != 0:
        log(f"[WARN] blood fetch rc={proc.returncode} (non-fatal, staging retained)", fh)
    else:
        log(f"blood fetch done in {time.time()-t0:.0f}s", fh)


def run_verification(fh):
    """件数チェック: staging results件数が prod 比 COUNT_DROP_LIMIT 以上減ったらNG。
    BT実行は行わない (261秒かかる上にROI変動で誤検知が多発していた)。
    """
    prod_count = _db_results_count(DB_PROD)
    stg_count  = _db_results_count(DB_STAGING)
    prod_max   = _db_max_date(DB_PROD)
    stg_max    = _db_max_date(DB_STAGING)
    log(f"verify: prod={prod_count}件({prod_max}) staging={stg_count}件({stg_max})", fh)

    if prod_count > 0:
        drop_rate = (prod_count - stg_count) / prod_count
        if drop_rate > COUNT_DROP_LIMIT:
            log(f"[FAIL] 件数減少 {drop_rate:.1%} (上限{COUNT_DROP_LIMIT:.0%}): staging={stg_count} prod={prod_count}", fh)
            return False, {"prod_count": prod_count, "stg_count": stg_count, "drop_rate": drop_rate}

    log(f"[OK] 件数チェック通過: staging={stg_count}件 prod={prod_count}件", fh)
    return True, {"prod_count": prod_count, "stg_count": stg_count, "drop_rate": 0.0}


def atomic_swap(fh):
    """staging → 本番 を os.replace (Windowsでもatomic)."""
    # 本番 wal/shm を綺麗にしてからreplace
    if DB_PROD.exists():
        c = sqlite3.connect(str(DB_PROD))
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            c.close()
        for suffix in ("-wal", "-shm"):
            side = DB_PROD.with_name(DB_PROD.name + suffix)
            if side.exists():
                side.unlink()
    log(f"ATOMIC swap: {DB_STAGING.name} → {DB_PROD.name}", fh)
    os.replace(str(DB_STAGING), str(DB_PROD))
    # stagingのwal/shmは破棄
    for suffix in ("-wal", "-shm"):
        side = DB_STAGING.with_name(DB_STAGING.name + suffix)
        if side.exists():
            side.unlink()


def _discord_error(content: str):
    """Discordにエラーアラートを送信 (失敗しても握りつぶす)"""
    try:
        from dotenv import load_dotenv as _ld
        _ld(ROOT / ".env")
    except Exception:
        pass
    # エラー専用webhook → なければ通常webhookにフォールバック
    webhook = os.environ.get("DISCORD_ERROR_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook:
        return
    try:
        import urllib.request as _ur, json as _json
        payload = {"content": content[:1900]}
        req = _ur.Request(webhook,
                          data=_json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"},
                          method="POST")
        _ur.urlopen(req, timeout=10)
    except Exception:
        pass


def alert(msg):
    # ① alerts_log に記録 (UI表示用)
    try:
        sys.path.insert(0, str(ROOT))
        import alerts_log  # type: ignore
        if hasattr(alerts_log, "write_alert"):
            alerts_log.write_alert(msg)
    except Exception:
        try:
            with open(ROOT / "alerts.log", "a", encoding="utf-8") as f:
                f.write(f"[{dt.datetime.now().isoformat()}] {msg}\n")
        except Exception:
            pass
    # ② Discord通知 (エラーアラート)
    _discord_error(f"🚨 **JRAパイプライン エラー**\n{msg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="fromtime", default=None)
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--parallel", action="store_true",
                    help="Phase 2 並行運用モード: swapせずstagingを保持、last_fetch_parallel.txtで独立管理")
    args = ap.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"fetch_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    with open(log_path, "w", encoding="utf-8") as fh:
        log(f"=== fetch_and_build start ===", fh)
        log(f"log: {log_path}", fh)
        try:
            fromtime = args.fromtime or load_last_fetch(parallel=args.parallel)
            log(f"fromtime={fromtime} (parallel={args.parallel})", fh)
            if not args.skip_fetch:
                run_fetch(fromtime, fh)
            else:
                log("--skip-fetch: reusing existing CSVs", fh)

            build_staging(fh)
            run_blood_fetch(fh)
            if args.parallel:
                # prod未swapのためここで直接書き込んでも安全(stagingはdiff用でprodに影響しない)
                run_jvlink_training_import(fh)
                log("--parallel: skip BT verification (staging is for diff, not prod)", fh)
                save_last_fetch(dt.datetime.now().strftime("%Y%m%d%H%M%S"), parallel=True)
                log("=== fetch_and_build DONE (parallel) ===", fh)
                return 0
            ok, metrics = run_verification(fh)
            if not ok:
                # prod未swapのためここで直接書き込んでも安全
                run_jvlink_training_import(fh)
                msg = (f"verification FAIL: 件数減少 {metrics['drop_rate']:.1%} "
                       f"staging={metrics['stg_count']} prod={metrics['prod_count']}")
                log("[FAIL] " + msg, fh)
                alert(msg)
                log("KEEPING production DB untouched", fh)
                return 2

            log(f"[OK] verification OK staging={metrics['stg_count']}件 prod={metrics['prod_count']}件", fh)
            if args.parallel:
                log("--parallel: staging保持、swapしない、last_fetch_parallel更新", fh)
                save_last_fetch(dt.datetime.now().strftime("%Y%m%d%H%M%S"), parallel=True)
                log("=== fetch_and_build DONE (parallel) ===", fh)
                return 0
            if args.dry_run:
                # prod未swapのためここで直接書き込んでも安全
                run_jvlink_training_import(fh)
                log("--dry-run: skipping swap", fh)
                return 0

            atomic_swap(fh)
            # atomic_swapの"後"にtraining importする(2026-07-13修正)。
            # staging cloneはswap前のprod状態からなので、training importをswap前に
            # 行うとその回のtraining importがswapで丸ごと上書き消去されるバグがあった
            # (2026-07-09〜07-13、5日間の調教データが毎晩書き込まれては消える状態だった)。
            run_jvlink_training_import(fh)
            log("prev_last3f fix start", fh)
            try:
                from fix_prev_last3f import fix_prev_last3f as _fix_l3f
                _fix_l3f(DB_PROD)
            except Exception as _e:
                log(f"[WARN] prev_last3f fix error (non-fatal): {_e}", fh)
            log("prev_last3f fix done", fh)
            save_last_fetch(dt.datetime.now().strftime("%Y%m%d%H%M%S"))
            try:
                from generate_project_status import generate as _gen_ps
                _gen_ps('fetch_and_build')
            except Exception:
                pass
            log("=== fetch_and_build DONE ===", fh)
            return 0
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            log(f"[FAIL] EXCEPTION: {e}\n{tb}", fh)
            alert(f"fetch_and_build exception: {e}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
