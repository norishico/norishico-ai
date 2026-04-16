"""run_monitoring.py - 週次実運用モニタリング実行 (Phase 2B)

パイプライン:
  1. monitor_actual.py を実行 → docs/monitoring_report.md 更新
  2. レポートを解析して判定ステータス (GREEN/YELLOW/RED/INSUFFICIENT) 抽出
  3. RED なら alerts_log にアラート記録
  4. datasource_status.html を再生成
  5. logs/monitoring_YYYYMMDD.json に状態スナップショット保存

使い方:
  py scripts/run_monitoring.py           # 通常実行
  py scripts/run_monitoring.py --force   # INSUFFICIENT時もアラート強制
"""
import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MONITOR_PY   = ROOT / "monitor_actual.py"
REPORT_MD    = ROOT / "docs" / "monitoring_report.md"
LOG_DIR      = ROOT / "logs"
MIN_SAMPLE   = 30  # N<30 は INSUFFICIENT


def run_monitor_actual():
    """monitor_actual.py を実行して標準出力と Markdown レポートを得る."""
    proc = subprocess.run(
        [sys.executable, str(MONITOR_PY)],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8"
    )
    return proc.returncode, proc.stdout, proc.stderr


def parse_report():
    """docs/monitoring_report.md から累積 ROI・損益・N を抽出."""
    if not REPORT_MD.exists():
        return None
    text = REPORT_MD.read_text(encoding="utf-8")
    res = {}
    m = re.search(r"実ROI:\s*\*\*([\d.]+)%", text)
    if m: res["roi"] = float(m.group(1))
    m = re.search(r"累計\s*([\d,]+)R", text)
    if m: res["n"] = int(m.group(1).replace(",", ""))
    m = re.search(r"累計損益:\s*\*\*([+\-\d,]+)\s*円", text)
    if m: res["profit"] = int(m.group(1).replace(",", "").replace("+", ""))
    # 判定
    if "🟢+" in text or "🟢 想定超え" in text:
        res["verdict"] = "GREEN+"
    elif "🟢 正常" in text:
        res["verdict"] = "GREEN"
    elif "🟡" in text:
        res["verdict"] = "YELLOW"
    elif "🔴" in text:
        res["verdict"] = "RED"
    else:
        res["verdict"] = "UNKNOWN"
    # N fallback from cumulative block
    if "n" not in res:
        m = re.search(r"累積\s*(\d+)R", text)
        if m: res["n"] = int(m.group(1))
    return res


def apply_noise_filter(rep):
    """F1 2024 の教訓: サンプル不足時は判定を保留."""
    if rep is None:
        return "NO_DATA"
    if rep.get("n", 0) < MIN_SAMPLE:
        return "INSUFFICIENT"
    return rep["verdict"]


def write_alert(msg):
    try:
        sys.path.insert(0, str(ROOT))
        import alerts_log  # type: ignore
        if hasattr(alerts_log, "write_alert"):
            alerts_log.write_alert(msg)
            return True
    except Exception:
        pass
    with open(ROOT / "alerts.log", "a", encoding="utf-8") as f:
        f.write(f"[{dt.datetime.now().isoformat()}] {msg}\n")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    rc, out, err = run_monitor_actual()
    if rc != 0:
        print(f"[ERR] monitor_actual.py failed rc={rc}")
        print(err)
        write_alert(f"monitoring pipeline error: monitor_actual rc={rc}")
        return 1

    print(out)
    rep = parse_report()
    effective = apply_noise_filter(rep)

    snapshot = {
        "generated_at": dt.datetime.now().isoformat(),
        "parsed": rep,
        "effective_verdict": effective,
        "min_sample_threshold": MIN_SAMPLE,
    }
    (LOG_DIR / f"monitoring_{stamp}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    (LOG_DIR / "monitoring_latest.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== effective verdict: {effective} ===")

    if effective == "RED" or (effective == "INSUFFICIENT" and args.force):
        msg = (f"monitoring verdict={effective} "
               f"ROI={(rep or {}).get('roi', '?')}% "
               f"N={(rep or {}).get('n', '?')} "
               f"profit={(rep or {}).get('profit', '?')}")
        write_alert(msg)
        print(f"[ALERT] {msg}")

    # ダッシュボード再生成
    try:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "generate_datasource_dashboard.py")],
            cwd=str(ROOT), check=False)
    except Exception as e:
        print(f"[warn] dashboard regen failed: {e}")

    if effective == "RED":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
