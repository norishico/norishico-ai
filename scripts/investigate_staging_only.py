"""investigate_staging_only.py - staging専有レースの内訳調査

keiba_staging.db にあって keiba.db にないレースを
  日付 / 会場 / クラス / 開催区分 / 買い判定相当の有無
で層別し、netkeiba取り込み漏れの性質を判別する。
"""
import io
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DB_PROD    = ROOT / "keiba.db"
DB_STAGING = ROOT / "keiba_staging.db"


def pragma(c):
    c.execute("PRAGMA cache_size=-65536")
    c.execute("PRAGMA temp_store=MEMORY")


def grade_of(rn):
    s = str(rn or "")
    for g in ("G1", "G2", "G3"):
        if g in s: return g
    if "(L)" in s or "（L）" in s: return "3勝(L)"
    if "3勝" in s or "1600万" in s: return "3勝"
    if "2勝" in s or "1000万" in s: return "2勝"
    if "1勝" in s or "500万" in s:  return "1勝"
    if "未勝利" in s: return "未勝利"
    if "新馬" in s: return "新馬"
    if "障害" in s: return "障害"
    return "?"


def main():
    prod = sqlite3.connect(str(DB_PROD)); pragma(prod); prod.row_factory = sqlite3.Row
    stg  = sqlite3.connect(str(DB_STAGING)); pragma(stg); stg.row_factory = sqlite3.Row

    # 最近30日の staging専有 race_id を抽出
    since = "2026-03-16"

    prod_ids = {r[0] for r in prod.execute(
        "SELECT DISTINCT race_id FROM results WHERE date >= ?", (since,)
    )}
    stg_ids = {r[0] for r in stg.execute(
        "SELECT DISTINCT race_id FROM results WHERE date >= ?", (since,)
    )}
    only_stg = stg_ids - prod_ids
    only_prod = prod_ids - stg_ids

    print(f"=== staging専有レース調査 ({since} 以降) ===")
    print(f"prod(netkeiba)  {len(prod_ids):>4}R")
    print(f"staging(JV-Link) {len(stg_ids):>4}R")
    print(f"staging専有      {len(only_stg):>4}R")
    print(f"prod専有         {len(only_prod):>4}R")
    print()

    # staging専有の詳細取得
    rows = []
    for rid in only_stg:
        r = stg.execute("""
            SELECT race_id, date, venue, race_num, race_name,
                   num_horses, surface, distance, track_cond
            FROM results WHERE race_id=? LIMIT 1
        """, (rid,)).fetchone()
        if r:
            rows.append(dict(r))

    # 1. 日付別
    print("--- 日付別 ---")
    by_date = Counter(r["date"] for r in rows)
    for d, n in sorted(by_date.items()):
        print(f"  {d}: {n}R")
    print()

    # 2. 会場別
    print("--- 会場別 ---")
    by_venue = Counter(r["venue"] for r in rows)
    for v, n in sorted(by_venue.items(), key=lambda x: -x[1]):
        print(f"  {v}: {n}R")
    print()

    # 3. クラス別
    print("--- クラス別 ---")
    by_grade = Counter(grade_of(r["race_name"]) for r in rows)
    for g, n in sorted(by_grade.items(), key=lambda x: -x[1]):
        print(f"  {g}: {n}R")
    print()

    # 4. 障害・芝ダの分布
    print("--- 馬場種別 ---")
    by_surface = Counter(r["surface"] for r in rows)
    for s, n in sorted(by_surface.items(), key=lambda x: -x[1]):
        print(f"  {s}: {n}R")
    print()

    # 5. 詳細サンプル先頭20件
    print("--- サンプル(最大20件) ---")
    for r in sorted(rows, key=lambda x: (x["date"], x["venue"], x["race_num"]))[:20]:
        g = grade_of(r["race_name"])
        print(f"  {r['date']} {r['venue']}{r['race_num']:>2}R "
              f"[{g:<8}] {str(r['race_name'])[:30]:<30} "
              f"{r['num_horses']}頭 {r['surface']}{r['distance']}m")
    print()

    # 6. 買い判定が出ていたはずのレース判定
    # weekend_predictions.json に該当レースがあったか確認
    import json
    wp = ROOT / "weekend_predictions.json"
    if wp.exists():
        try:
            preds = json.loads(wp.read_text(encoding="utf-8"))
            pred_rids = set()
            for p in preds:
                r = p.get("race", {})
                rid = r.get("race_id")
                if rid:
                    pred_rids.add(rid)
            overlap = only_stg & pred_rids
            print(f"--- weekend_predictions.json との突合 ---")
            print(f"  staging専有 ∩ 現在の予想対象: {len(overlap)}R")
            if overlap:
                print("  → 予想に入っていたレースが漏れている可能性:")
                for rid in sorted(overlap)[:10]:
                    for p in preds:
                        if p.get("race", {}).get("race_id") == rid:
                            r = p["race"]
                            bt = p.get("buy_type") or p.get("special_horse", {}).get("rule", "")
                            print(f"    {rid} {r.get('venue')}{r.get('race_num')}R "
                                  f"{r.get('race_name','')[:25]} buy={bt}")
                            break
        except Exception as e:
            print(f"weekend_predictions 読込失敗: {e}")
    print()

    # 7. 障害・ローカル開催の割合判定
    jumps = sum(1 for r in rows if "障" in str(r.get("surface","")) or "障害" in str(r.get("race_name","")))
    print(f"--- フィルタ対象判定 ---")
    print(f"  障害レース: {jumps}R ({jumps/len(rows)*100:.1f}%)")

    prod.close(); stg.close()


if __name__ == "__main__":
    main()
