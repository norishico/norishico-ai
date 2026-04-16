"""diff_sources.py - Phase 2 並行検証: keiba.db (netkeiba) vs keiba_staging.db (JV-Link)

目的:
  1. 最近 N 日分の race_id 集合差
  2. (race_id, horse_num) 単位で値ズレ検知
  3. 合意基準 (docs/JVLINK_SCHEMA_DIFF.md) との照合
  4. JSON レポート出力 + アラート判定

出力:
  logs/diff_report_YYYYMMDD_HHMMSS.json
  logs/diff_report_latest.json  (ダッシュボード参照用)

使い方:
  python scripts/diff_sources.py                 # 直近30日
  python scripts/diff_sources.py --days 7
  python scripts/diff_sources.py --quiet         # 標準出力を抑制
"""

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PROD    = ROOT / "keiba.db"
DB_STAGING = ROOT / "keiba_staging.db"
LOG_DIR    = ROOT / "logs"

# 合意基準 (docs/JVLINK_SCHEMA_DIFF.md)
AGREEMENT_CRITERIA = {
    "race_set_diff_max": 0,              # レース集合差は0であるべき
    "value_match_rate_min": 0.99,        # 重要値(finish/odds/time_raw等)の一致率
    "blood_match_rate_min": 0.95,        # 血統表記の一致率(共通行)
    "prize_won_null_max": 0.20,          # prize_won の NULL率上限
    "staging_blood_null_max": 0.05,      # staging全行の血統NULL率(新規行含む)
}

# 値ズレ検知対象列 (スコアリングで実使用)
VALUE_CHECK_COLS = [
    "finish", "odds", "time_raw", "last3f",
    "pos1", "pos2", "pos3", "pos4", "horse_weight", "popularity",
]
BLOOD_CHECK_COLS = ["sire", "dam", "dam_sire"]


def pragma(conn):
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")


def fetch_race_set(conn, since_date):
    cur = conn.execute(
        "SELECT DISTINCT race_id FROM results WHERE date >= ?",
        (since_date,),
    )
    return {r[0] for r in cur.fetchall()}


def fetch_rows(conn, since_date):
    cols = ",".join(["race_id", "horse_name"] + VALUE_CHECK_COLS + BLOOD_CHECK_COLS + ["prize_won"])
    cur = conn.execute(
        f"SELECT {cols} FROM results WHERE date >= ?",
        (since_date,),
    )
    out = {}
    for row in cur.fetchall():
        key = (row[0], (row[1] or "").strip())
        out[key] = row
    return out


def compare(since_date, quiet=False):
    if not DB_PROD.exists():
        raise RuntimeError(f"prod DB not found: {DB_PROD}")
    if not DB_STAGING.exists():
        raise RuntimeError(f"staging DB not found: {DB_STAGING} (Step 2 の --parallel 実行後に再試行)")

    prod = sqlite3.connect(str(DB_PROD)); pragma(prod)
    stg  = sqlite3.connect(str(DB_STAGING)); pragma(stg)

    # 1. レース集合差
    r_prod = fetch_race_set(prod, since_date)
    r_stg  = fetch_race_set(stg,  since_date)
    only_prod = sorted(r_prod - r_stg)
    only_stg  = sorted(r_stg - r_prod)
    common_races = r_prod & r_stg

    # 2. 行単位の値ズレ
    rows_p = fetch_rows(prod, since_date)
    rows_s = fetch_rows(stg,  since_date)
    common_keys = set(rows_p.keys()) & set(rows_s.keys())

    val_total = len(common_keys) * len(VALUE_CHECK_COLS)
    val_match = 0
    val_mismatch_samples = []
    blood_total = len(common_keys) * len(BLOOD_CHECK_COLS)
    blood_match = 0
    prize_null_stg = 0

    for k in common_keys:
        pr = rows_p[k]; sr = rows_s[k]
        # pr[0]=race_id, pr[1]=horse_name, pr[2..] = VALUE_CHECK + BLOOD_CHECK + prize_won
        base = 2
        for i, col in enumerate(VALUE_CHECK_COLS):
            a = pr[base + i]; b = sr[base + i]
            if _eq(a, b):
                val_match += 1
            elif len(val_mismatch_samples) < 20:
                val_mismatch_samples.append({
                    "race_id": k[0], "horse": k[1], "col": col,
                    "prod": a, "staging": b,
                })
        base2 = base + len(VALUE_CHECK_COLS)
        for i, col in enumerate(BLOOD_CHECK_COLS):
            a = (pr[base2 + i] or "").strip()
            b = (sr[base2 + i] or "").strip()
            if a == b:
                blood_match += 1
        prize_col = base2 + len(BLOOD_CHECK_COLS)
        if sr[prize_col] in (None, "", 0):
            prize_null_stg += 1

    val_rate = val_match / val_total if val_total else 1.0
    blood_rate = blood_match / blood_total if blood_total else 1.0
    prize_null_rate = prize_null_stg / len(common_keys) if common_keys else 0.0

    # staging全行(新規取得含む)の血統NULL率
    # 共通行しか見ない blood_match_rate では捕らえられない
    # "JV-Linkで新しく取得した行がちゃんと血統埋まってるか"を検知
    stg_total = stg.execute(
        "SELECT COUNT(*) FROM results WHERE date >= ?", (since_date,)
    ).fetchone()[0]
    stg_blood_null = stg.execute(
        "SELECT COUNT(*) FROM results WHERE date >= ? AND (sire IS NULL OR sire='')",
        (since_date,),
    ).fetchone()[0]
    stg_blood_null_rate = stg_blood_null / stg_total if stg_total else 0.0

    report = {
        "generated_at": dt.datetime.now().isoformat(),
        "since_date": since_date,
        "race_set": {
            "prod_count": len(r_prod),
            "staging_count": len(r_stg),
            "common_count": len(common_races),
            "only_in_prod": only_prod[:50],
            "only_in_prod_total": len(only_prod),
            "only_in_staging": only_stg[:50],
            "only_in_staging_total": len(only_stg),
        },
        "row_compare": {
            "common_rows": len(common_keys),
            "value_match_rate": round(val_rate, 4),
            "blood_match_rate": round(blood_rate, 4),
            "staging_prize_null_rate": round(prize_null_rate, 4),
            "staging_total_rows": stg_total,
            "staging_blood_null_count": stg_blood_null,
            "staging_blood_null_rate": round(stg_blood_null_rate, 4),
            "value_mismatch_samples": val_mismatch_samples,
        },
        "criteria": AGREEMENT_CRITERIA,
        "judgement": {
            "race_set_ok": (len(only_prod) + len(only_stg)) <= AGREEMENT_CRITERIA["race_set_diff_max"],
            "value_ok": val_rate >= AGREEMENT_CRITERIA["value_match_rate_min"],
            "blood_ok": blood_rate >= AGREEMENT_CRITERIA["blood_match_rate_min"],
            "prize_null_ok": prize_null_rate <= AGREEMENT_CRITERIA["prize_won_null_max"],
            "stg_blood_null_ok": stg_blood_null_rate <= AGREEMENT_CRITERIA["staging_blood_null_max"],
        },
    }
    report["judgement"]["overall_ok"] = all(report["judgement"][k] for k in
        ("race_set_ok", "value_ok", "blood_ok", "prize_null_ok", "stg_blood_null_ok"))

    prod.close(); stg.close()

    if not quiet:
        _print(report)
    return report


def _eq(a, b):
    if a is None and b is None: return True
    if a is None or b is None: return False
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-6
        except (TypeError, ValueError):
            return False
    return str(a).strip() == str(b).strip()


def _print(rep):
    rs = rep["race_set"]; rc = rep["row_compare"]; j = rep["judgement"]
    print(f"=== JV-Link 並行検証レポート ({rep['since_date']} 以降) ===")
    print(f"レース数  prod={rs['prod_count']} staging={rs['staging_count']} common={rs['common_count']}")
    print(f"  prod専有={rs['only_in_prod_total']}  staging専有={rs['only_in_staging_total']}")
    print(f"共通行数 {rc['common_rows']}")
    print(f"  値一致率   {rc['value_match_rate']*100:.2f}%  (基準 99%)")
    print(f"  血統一致率 {rc['blood_match_rate']*100:.2f}%  (基準 95%)")
    print(f"  stg prize_won NULL率 {rc['staging_prize_null_rate']*100:.2f}%  (基準 20%)")
    print(f"  stg 血統NULL率(全行) {rc['staging_blood_null_rate']*100:.2f}%  "
          f"({rc['staging_blood_null_count']}/{rc['staging_total_rows']}) (基準 5%)")
    print(f"判定: race_set={j['race_set_ok']} value={j['value_ok']} "
          f"blood={j['blood_ok']} prize_null={j['prize_null_ok']} "
          f"stg_blood_null={j['stg_blood_null_ok']} "
          f"=> overall={'OK' if j['overall_ok'] else 'NG'}")
    if rc["value_mismatch_samples"]:
        print("値ズレサンプル(先頭5件):")
        for s in rc["value_mismatch_samples"][:5]:
            print(f"  {s['race_id']} {s['horse']} {s['col']}: prod={s['prod']} stg={s['staging']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    since = (dt.datetime.now() - dt.timedelta(days=args.days)).strftime("%Y-%m-%d")
    LOG_DIR.mkdir(exist_ok=True)

    try:
        rep = compare(since, quiet=args.quiet)
    except Exception as e:
        err = {"error": str(e), "generated_at": dt.datetime.now().isoformat()}
        (LOG_DIR / "diff_report_latest.json").write_text(
            json.dumps(err, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ERR] {e}", file=sys.stderr)
        return 1

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = LOG_DIR / f"diff_report_{stamp}.json"
    latest = LOG_DIR / "diff_report_latest.json"
    payload = json.dumps(rep, ensure_ascii=False, indent=2)
    out.write_text(payload, encoding="utf-8")
    latest.write_text(payload, encoding="utf-8")

    return 0 if rep["judgement"]["overall_ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
