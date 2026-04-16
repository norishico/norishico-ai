"""fetch_and_build.py - 自動データパイプライン (64bit Python)

JV-Link → TARGET CSV → staging DB → バックテスト検証 → ATOMIC swap

フロー:
  1. last_fetch.txt から前回取得時刻を読み、無ければ 7日前
  2. 32bit python で jvlink_fetch.py を実行 → jvlink_dump_results.csv / _dividends.csv
  3. keiba.db を keiba_staging.db に複製
  4. build_db.build_db(csvs, db_path="keiba_staging.db") で差分流し込み
  5. staging を keiba_tmp_2025.db に複製して backtest_v6 --year 2025 実行
  6. btv6_2025.json の ROI が 許容帯内なら ATOMIC swap で本番昇格
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

# 検証基準: v6.6 2025年ゴールデン = 336R / 99.6% / -1,600
VERIFY_YEAR = 2025
ROI_MIN = 85.0        # 下限 (構造破壊の早期検知)
ROI_MAX = 115.0       # 上限
NBET_MIN = 280        # 買い目発動件数下限 (C2/F1含む)
NBET_MAX = 400


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


def run_fetch(fromtime, fh):
    if not PY32.exists():
        raise RuntimeError(f"32bit Python not found: {PY32}")
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


def build_staging(fh):
    """keiba.db → keiba_staging.db 複製 → CSV 流し込み."""
    if DB_PROD.exists():
        # WAL checkpoint してから複製
        c = sqlite3.connect(str(DB_PROD))
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            c.close()
        log(f"clone {DB_PROD.name} → {DB_STAGING.name}", fh)
        if DB_STAGING.exists():
            DB_STAGING.unlink()
        for suffix in ("", "-wal", "-shm"):
            src = DB_PROD.with_name(DB_PROD.name + suffix)
            if src.exists():
                shutil.copy2(src, DB_STAGING.with_name(DB_STAGING.name + suffix))
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
    """staging DB で backtest_v6 --year VERIFY_YEAR を実行し、ROI 許容帯を判定."""
    tmp_db = ROOT / f"keiba_tmp_{VERIFY_YEAR}.db"
    if tmp_db.exists():
        tmp_db.unlink()
    shutil.copy2(DB_STAGING, tmp_db)
    log(f"backtest_v6 --year {VERIFY_YEAR} on staging", fh)
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "backtest_v6.py"), "--year", str(VERIFY_YEAR)],
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
        raise RuntimeError(f"backtest_v6 failed rc={proc.returncode}")
    log(f"backtest done in {time.time()-t0:.0f}s", fh)

    report = ROOT / f"btv6_{VERIFY_YEAR}.json"
    if not report.exists():
        raise RuntimeError(f"report not found: {report}")
    with open(report, "r", encoding="utf-8") as f:
        data = json.load(f)
    s = data.get("summary", {})
    roi = float(s.get("roi", 0))
    n_bet = int(s.get("n_bet", 0))
    prof = int(s.get("profit", 0))
    log(f"verify result: n_bet={n_bet} ROI={roi:.1f}% profit={prof:+,}", fh)

    ok = (ROI_MIN <= roi <= ROI_MAX) and (NBET_MIN <= n_bet <= NBET_MAX)
    return ok, {"roi": roi, "n_bet": n_bet, "profit": prof}


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


def alert(msg):
    try:
        sys.path.insert(0, str(ROOT))
        import alerts_log  # type: ignore
        if hasattr(alerts_log, "write_alert"):
            alerts_log.write_alert(msg)
            return
    except Exception:
        pass
    # fallback
    with open(ROOT / "alerts.log", "a", encoding="utf-8") as f:
        f.write(f"[{dt.datetime.now().isoformat()}] {msg}\n")


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
            run_jvlink_training_import(fh)
            run_blood_fetch(fh)
            if args.parallel:
                log("--parallel: skip BT verification (staging is for diff, not prod)", fh)
                save_last_fetch(dt.datetime.now().strftime("%Y%m%d%H%M%S"), parallel=True)
                log("=== fetch_and_build DONE (parallel) ===", fh)
                return 0
            ok, metrics = run_verification(fh)
            if not ok:
                msg = (f"verification FAIL year={VERIFY_YEAR} "
                       f"ROI={metrics['roi']:.1f}% (expect {ROI_MIN}-{ROI_MAX}) "
                       f"n_bet={metrics['n_bet']} (expect {NBET_MIN}-{NBET_MAX})")
                log("[FAIL] " + msg, fh)
                alert(msg)
                log("KEEPING production DB untouched", fh)
                return 2

            log(f"[OK] verification OK ROI={metrics['roi']:.1f}% n_bet={metrics['n_bet']}", fh)
            if args.parallel:
                log("--parallel: staging保持、swapしない、last_fetch_parallel更新", fh)
                save_last_fetch(dt.datetime.now().strftime("%Y%m%d%H%M%S"), parallel=True)
                log("=== fetch_and_build DONE (parallel) ===", fh)
                return 0
            if args.dry_run:
                log("--dry-run: skipping swap", fh)
                return 0

            atomic_swap(fh)
            save_last_fetch(dt.datetime.now().strftime("%Y%m%d%H%M%S"))
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
