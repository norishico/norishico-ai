# -*- coding: utf-8 -*-
"""
generate_mc123_forecast.py — AYOkeibaサイト用のMC123情報提供データ生成(2026-08-08新設)

指定日の全レース(新馬戦・障害を除く、芝・ダート全レース)について、MC123エンジン
(mc123_engine.run_mc123)で各馬のp1/p2/p3/ptop3確率を算出し、
mc_keiba_public/mc123_data.json に書き出す。

設計方針(のりお承認 2026-08-08、/committee経由):
- 買い目としては提示しない、情報提供専用。MC123は「賭けて勝てるか」の検証
  (mc_dyn×MC123ライン、4年WF CV ROI61.0%)では不採用が確定済みだが、順位付けの精度
  (Brier score改善、2023-2025年OOSで3年とも改善、calibration_result.json)は別軸で
  確認済みのため、情報提供としてのみ表示する。オッズ(期待値)は考慮しない
- generate_pace_forecast.pyと同じデータ経路(既存RACES/widget_data.jsonとは完全独立)。
  本スクリプトが失敗しても本体サイトのRACES表示・展開予想には一切影響しない
- 対象は展開予想タブと同一基準(芝・ダート全レース、新馬・障害のみ除外。未勝利は含む)
- 馬場3パターン(良・稍重/重/不良)を算出し、展開予想タブと同じ馬場ピルで連動切替できる
  ようにする
- 当日レース未確定(JV-Link未反映)時はthis_week_races.jsonからのライブフォールバックに
  対応(generate_pace_forecast.pyのfetch_day_races/fetch_day_races_liveをそのまま再利用)
- 較正済み8係数(K_ABILITY等)はmc123_engine.pyの本番値をそのまま使用(上書きしない)。
  構造テーブル(class_par等)はcutoff_date=対象日で毎回フレッシュ構築(未来レース予測の
  ためリークの概念自体がなく、永続化より鮮度優先)

使い方: py -3 generate_mc123_forecast.py [YYYY-MM-DD]
"""
import sys
import json
import time
import sqlite3
from datetime import date as _date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_pace_forecast import (
    fetch_day_races, fetch_day_races_live, pace_cls_group, umaban_to_waku, _load_live_races,
)
from build_class_par import build_class_par_table, calibrate_k_cls
from build_pace_baseline import build_baseline_table
from build_extra_par import build_rank_par, build_margin_par, build_l3f_par
from mc123_batch import load_horse_hist_all, load_same_day_bias_dict, precompute_horse_features_fast
from mc123_engine import run_mc123, hash64_seed
from generate_race_sim import classify_style_c2

DB = "keiba.db"
OUT_PATHS = [Path("mc_keiba_public/mc123_data.json")]
TARGET_DATE = sys.argv[1] if len(sys.argv) > 1 else _date.today().isoformat()
N_MC = 10000
TRACK_PATTERNS = [("良・稍重", "良"), ("重", "重"), ("不良", "不")]

# 2026-08-10新規/2026-08-13統計手法改訂: 会場×距離別のMC123 1位予想 複勝的中率の信頼性
# (12人委員会承認、compute_mc123_top1_reliability.pyの出力をそのまま参照)。
# 見つからないセルのみNoneを返す。n<15セルも縮小値を返すがdata_limited=Trueを付与し、
# フロント側で「データ限定的」の注記を出す(旧版はreliable=false/heterogeneous=falseなら
# 一律非表示だったが、シュリンク自体は常時適用・小標本は注記付きで見せる方針に変更)
_RELIABILITY_PATH = Path(__file__).resolve().parent / "mc123_top1_reliability.json"
_RELIABILITY_CACHE = None


def load_reliability_lookup():
    global _RELIABILITY_CACHE
    if _RELIABILITY_CACHE is not None:
        return _RELIABILITY_CACHE
    if not _RELIABILITY_PATH.exists():
        _RELIABILITY_CACHE = {}
        return _RELIABILITY_CACHE
    data = json.loads(_RELIABILITY_PATH.read_text(encoding="utf-8"))
    lut = {(c["venue"], c["surface"], c["distance"]): c for c in data["cells"]}
    lut["__meta__"] = {"heterogeneous": data["heterogeneous"], "n_races_used": data["n_races_used"]}
    _RELIABILITY_CACHE = lut
    return lut


def get_top1_reliability_entry(venue, surface, distance):
    lut = load_reliability_lookup()
    c = lut.get((venue, surface, distance))
    if not c:
        return None
    return {
        "tier": c["tier"], "place_rate": c["place_rate_shrunk"], "n": c["n"],
        "data_limited": bool(c.get("data_limited")),
    }


def umaban_to_gate(umaban):
    if not umaban or umaban <= 0:
        return 4
    return min((umaban + 1) // 2, 8)


def fetch_horses_for_mc123(conn, race_id, live_mode):
    """MC123用のhorses(horse_name/umaban/jockey/gate/style/finish)を組み立てる。"""
    if live_mode:
        live = _load_live_races()
        r = live.get(race_id)
        if r is None:
            return []
        rows = r.get("horses", [])
        out = []
        for hi, h in enumerate(rows):
            uma = h.get("umaban") or (hi + 1)
            out.append({"horse_name": (h.get("name") or "").strip(), "umaban": uma,
                        "jockey": (h.get("jockey") or "").strip(), "gate": umaban_to_gate(uma),
                        "style": None, "finish": None})
        return out
    rows = conn.execute("""
        SELECT TRIM(horse_name), jockey, umaban
        FROM results WHERE race_id = ? AND (finish IS NULL OR finish < 90)
        ORDER BY (umaban IS NULL), umaban, horse_name
    """, (race_id,)).fetchall()
    out = []
    for hi, r in enumerate(rows):
        uma = r[2] or (hi + 1)
        out.append({"horse_name": r[0], "umaban": uma, "jockey": r[1] or "",
                    "gate": umaban_to_gate(uma), "style": None, "finish": None})
    return out


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    day_races = fetch_day_races(conn)
    live_mode = False
    if not day_races:
        day_races = fetch_day_races_live()
        live_mode = bool(day_races)
        if live_mode:
            print(f"{TARGET_DATE}: resultsに未反映のためthis_week_races.jsonのライブ経路を使用")
    print(f"{TARGET_DATE}: {len(day_races)}レース" + ("(ライブ)" if live_mode else ""))

    print("構造テーブル構築中...")
    t0 = time.time()
    horse_hist = load_horse_hist_all(conn)
    bias_map = load_same_day_bias_dict(conn)
    class_par = build_class_par_table(conn, cutoff_date=TARGET_DATE, verbose=False)
    k_cls = calibrate_k_cls(conn, cutoff_date=TARGET_DATE, verbose=False)
    pace_baseline = build_baseline_table(conn, cutoff_date=TARGET_DATE, verbose=False)
    rank_par = build_rank_par(conn, cutoff_date=TARGET_DATE, verbose=False)
    margin_par = build_margin_par(conn, cutoff_date=TARGET_DATE, verbose=False)
    l3f_par = build_l3f_par(conn, cutoff_date=TARGET_DATE, verbose=False)
    print(f"構築完了({time.time()-t0:.1f}秒)")

    races_out, n_skip_shinba, n_skip_jump, n_skip_err = [], 0, 0, 0
    for race_id, venue, rno, rname, surface, distance, n_ent, track_cond in day_races:
        if pace_cls_group(rname) == "新馬":
            n_skip_shinba += 1
            continue
        if "障害" in (rname or "") or surface not in ("芝", "ダ"):
            n_skip_jump += 1
            continue
        horses = fetch_horses_for_mc123(conn, race_id, live_mode)
        if len(horses) < 3:
            n_skip_err += 1
            continue

        # 脚質(表示用+MC123内部のn_nige/n_front算出に必須。展開予想タブと同じclassify_style_c2)
        pf_horses = [{"horse_name": h["horse_name"], "jockey": h["jockey"], "umaban": h["umaban"]}
                     for h in horses]
        style_race = {"date": TARGET_DATE, "venue": venue, "surface": surface, "distance": distance,
                      "num_horses": len(horses), "track_cond": track_cond, "race_name": rname}
        try:
            c2 = classify_style_c2(conn, style_race, pf_horses)
            for h, c in zip(horses, c2):
                h["style"] = c["style"]
        except Exception as e:
            print(f"  ERR {race_id} {rname} (脚質分類): {e}")
            n_skip_err += 1
            continue

        n = len(horses)
        patterns, first_result = {}, None
        for label, cond in TRACK_PATTERNS:
            race_info = {"venue": venue, "distance": distance, "track_cond": cond,
                        "num_horses": n, "date": TARGET_DATE, "surface": surface, "race_id": race_id}
            try:
                precompute_horse_features_fast(horses, race_info, horse_hist, class_par, k_cls,
                                                bias_map, pace_baseline, rank_par=rank_par,
                                                margin_par=margin_par, l3f_par=l3f_par)
                seed = hash64_seed(f"{race_id}_{cond}")
                result = run_mc123(horses, race_info, n_mc=N_MC, seed=seed, wind=None)
            except Exception as e:
                print(f"  ERR {race_id} {rname} ({label}): {e}")
                result = None
            if result is None:
                continue
            if first_result is None:
                first_result = result
            patterns[label] = [{"p1": round(r["p1"], 4), "p2": round(r["p2"], 4),
                               "p3": round(r["p3"], 4), "ptop3": round(r["ptop3"], 4)}
                              for r in result]
        if first_result is None:
            n_skip_err += 1
            continue

        order = sorted(range(n), key=lambda i: -first_result[i]["ptop3"])
        horses_out = [{
            "num": horses[i]["umaban"], "waku": umaban_to_waku(horses[i]["umaban"], n),
            "name": horses[i]["horse_name"], "jockey": horses[i]["jockey"],
            "style": horses[i]["style"], "rank": rank + 1,
            "patterns": {label: patterns[label][i] for label in patterns},
        } for rank, i in enumerate(order)]

        races_out.append({
            "race_id": race_id, "venue": venue, "rno": rno, "rname": rname,
            "surface": surface, "distance": distance, "n_horses": n,
            "horses": horses_out,
            "top1_reliability": get_top1_reliability_entry(venue, surface, distance),
        })
        print(f"  OK {venue}{rno}R {rname} {surface}{distance}m {n}頭")

    payload = {
        "date": TARGET_DATE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_mc": N_MC,
        "note": "MC123エンジンによる各馬の1〜3着内確率です。買い目(推奨馬券)ではなく参考情報です。"
                "オッズ(期待値)は考慮していません。",
        "races": races_out,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    for p in OUT_PATHS:
        if p.parent.exists():
            p.write_text(text, encoding="utf-8")
            print(f"書き出し: {p}")
    print(f"完了: {len(races_out)}R 出力 / 新馬スキップ{n_skip_shinba} / 障害等スキップ{n_skip_jump} / "
          f"対象外・失敗{n_skip_err}")
    conn.close()


if __name__ == "__main__":
    main()
