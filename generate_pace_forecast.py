# -*- coding: utf-8 -*-
"""
generate_pace_forecast.py — AYOkeibaサイト「展開予想」タブ用のデータ生成(2026-08-07新設)。

指定日の全レース(新馬戦除く)について、mc_dyn(predict_race_formation.predict_formation)で
予想ペース(馬場3パターン: 良・稍重/重/不良)+予想隊列(スタート後/3角/4角/ゴール)を計算し、
mc_keiba_public/pace_data.json に書き出す。

設計方針(のりお承認 2026-08-07):
- 既存のwidget_data.json/RACES埋め込みとは完全に独立したファイル。本スクリプトが失敗しても
  本体サイト(index.htmlのRACES・払戻機能)には一切影響しない。
- 対象はAYO軸馬の有無によらない当日全レース。ただし新馬戦は除外
  (predict_formation側の既存ガードと同一基準: 隊列予測の実測精度ρ=0.289と低いため)。
- 枠番はJRA正式規則で馬番+頭数から算出(9-16頭は後ろの枠から2頭化、17-18頭はさらに
  7・8枠が3頭)。umaban欠損時は出走順の連番で代替し numbers_estimated=true を立てる
  (generate_mc_record.pyと同じフォールバック方針。UIで注記表示するため)。
- デプロイは別途手動(npx vercel deploy --prod)。本スクリプトはJSON生成まで。

使い方: py -3 generate_pace_forecast.py [YYYY-MM-DD]   (省略時=今日)
"""
import sys
import json
import sqlite3
from datetime import date as _date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from predict_race_formation import predict_formation, rank_to_tier, describe_pace, TIER_LABELS
from mc_dyn_engine import pace_cls_group

DB = "keiba.db"
OUT_PATHS = [Path("mc_keiba_public/pace_data.json")]
TARGET_DATE = sys.argv[1] if len(sys.argv) > 1 else _date.today().isoformat()
N_SIM = 5000

# 会場×表面×距離別の隊列予測精度(compute_formation_accuracy.py生成、2026-08-08)。
# 見つからない/信頼できない(n<15)セルはNoneのまま返し、フロント側で「データ不足」扱いする
_ACCURACY_PATH = Path(__file__).resolve().parent / "formation_accuracy.json"
_ACCURACY_CACHE = None


def load_accuracy_lookup():
    global _ACCURACY_CACHE
    if _ACCURACY_CACHE is not None:
        return _ACCURACY_CACHE
    if not _ACCURACY_PATH.exists():
        _ACCURACY_CACHE = {}
        return _ACCURACY_CACHE
    data = json.loads(_ACCURACY_PATH.read_text(encoding="utf-8"))
    lut = {}
    for c in data["cells"]:
        lut[(c["venue"], c["surface"], c["distance"])] = c
    lut["__meta__"] = {
        "computed_scope": data["computed_scope"], "n_races_used": data["n_races_used"],
        "grand_mean_rho_pos4": data["grand_mean_rho_pos4"], "grand_mean_rho_goal": data["grand_mean_rho_goal"],
        "heterogeneous": data["heterogeneous"],
    }
    _ACCURACY_CACHE = lut
    return lut


def get_accuracy_entry(venue, surface, distance):
    lut = load_accuracy_lookup()
    c = lut.get((venue, surface, distance))
    meta = lut.get("__meta__", {})
    if not c or not c.get("reliable") or not meta.get("heterogeneous"):
        return None
    return {
        "tier": c["tier"], "rho_pos4": c["rho_pos4_shrunk"], "rho_goal": c.get("rho_goal_raw"),
        "n": c["n"], "scope": meta.get("computed_scope"),
    }

# サイトの既存馬場pill(良・稍重/重/不良)とtrack_condの対応
TRACK_PATTERNS = [("良・稍重", "良"), ("重", "重"), ("不良", "不")]

# 隊列表示ステージ(predict_race_formation.cmd_raceのnetkeiba風3段+ゴール)
STAGE_MAP = [("start", "序盤(1角入口)"), ("corner3", "終盤(最終C入口≒3角)"),
             ("corner4", "直線入口(最終C出口≒4角)"), ("goal", "ゴール")]


def umaban_to_waku(u, n):
    """JRA正式規則の枠番算出。n<=8は枠=馬番、9-16頭は8枠側から順に2頭化、
    17-18頭はさらに8枠側から3頭化(例: 18頭は7枠13-15/8枠16-18)。"""
    if n <= 8:
        return u
    counts = [1] * 8
    i, rem = 7, n - 8
    while rem > 0:
        counts[i] += 1
        i -= 1
        if i < 0:
            i = 7
        rem -= 1
    c = 0
    for w, cnt in enumerate(counts, start=1):
        c += cnt
        if u <= c:
            return w
    return 8


def fetch_day_races(conn):
    rows = conn.execute("""
        SELECT race_id, venue, race_num, MAX(race_name), surface, distance,
               COUNT(*), MAX(track_cond)
        FROM results WHERE date = ?
        GROUP BY race_id ORDER BY venue, race_num
    """, (TARGET_DATE,)).fetchall()
    return rows


def fetch_horses(conn, race_id):
    """出走馬(未確定レースはfinish NULLのまま取得。取消・中止(finish>=90)のみ除外)。"""
    rows = conn.execute("""
        SELECT TRIM(horse_name), jockey, TRIM(sire), umaban, weight_kg,
               horse_weight, pos3, pos4, finish, pos1, pos2
        FROM results WHERE race_id = ? AND (finish IS NULL OR finish < 90)
        ORDER BY (umaban IS NULL), umaban, horse_name
    """, (race_id,)).fetchall()
    horses = []
    numbers_estimated = False
    for hi, r in enumerate(rows):
        uma = r[3]
        if not uma:
            uma = hi + 1
            numbers_estimated = True
        horses.append({"horse_name": r[0], "jockey": r[1] or "", "sire": r[2],
                       "umaban": uma, "weight_kg": r[4], "horse_weight": r[5],
                       "pos3": r[6], "pos4": r[7], "finish": r[8],
                       "pos1": r[9], "pos2": r[10]})
    return horses, numbers_estimated


# 【2026-08-08追加】当日ライブフォールバック: 開催当日はJV-Link経由のresults反映が
# レース終了後になるため、発走前は`results`にTARGET_DATE分の行が1件も無い。
# その間はauto_refresh.pyが管理するthis_week_races.json(出走表+想定オッズ)から
# 直接読む。results行が存在すればそちらを優先するため、この経路は本当に無い時のみ使う。
_LIVE_RACES_CACHE = None


def _load_live_races():
    global _LIVE_RACES_CACHE
    if _LIVE_RACES_CACHE is not None:
        return _LIVE_RACES_CACHE
    p = Path("this_week_races.json")
    if not p.exists():
        _LIVE_RACES_CACHE = {}
        return _LIVE_RACES_CACHE
    all_races = json.loads(p.read_text(encoding="utf-8"))
    today = [r for r in all_races if r.get("date") == TARGET_DATE]
    _LIVE_RACES_CACHE = {f"{TARGET_DATE}_{r.get('venue','')}_{r.get('race_num',0)}": r for r in today}
    return _LIVE_RACES_CACHE


def fetch_day_races_live():
    live = _load_live_races()
    out = []
    for race_id, r in live.items():
        out.append((race_id, r.get("venue", ""), r.get("race_num", 0), r.get("race_name") or "",
                    r.get("surface") or "", r.get("distance") or 0,
                    len(r.get("horses", [])), r.get("track_cond") or "良"))
    return sorted(out, key=lambda x: (x[1], x[2]))


def fetch_horses_live(race_id):
    """this_week_races.jsonの出走表から未確定レース用のhorsesを組み立てる。
    sire/horse_weightは省略(classify_style_c2側がDB直近値・欠損フラグで自動補完する)。"""
    live = _load_live_races()
    r = live.get(race_id)
    if r is None:
        return [], False
    horses, numbers_estimated = [], False
    for hi, h in enumerate(r.get("horses", [])):
        uma = h.get("umaban")
        if not uma:
            uma = hi + 1
            numbers_estimated = True
        try:
            wkg = float(h.get("weight", "") or "")
        except ValueError:
            wkg = None
        horses.append({"horse_name": (h.get("name") or "").strip(), "jockey": (h.get("jockey") or "").strip(),
                       "sire": None, "umaban": uma, "weight_kg": wkg, "horse_weight": None,
                       "pos3": None, "pos4": None, "finish": None, "pos1": None, "pos2": None})
    return horses, numbers_estimated


def _process_one_race(args):
    """1レース分の展開予想を計算しJSON辞書を返す(並列ワーカー、レース単位で独立)。"""
    race_id, venue, rno, rname, surface, distance, n_ent, track_cond, live_mode = args
    # Windows spawnワーカーでのstdout競合対策(compute_formation_accuracy.pyと同一の対処)
    import os as _os
    global _keep_alive_ref
    _keep_alive_ref = sys.stdout = open(_os.devnull, "w", encoding="utf-8")
    import sqlite3 as _sq
    conn = _sq.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        horses, numbers_estimated = (fetch_horses_live(race_id) if live_mode
                                      else fetch_horses(conn, race_id))
        if len(horses) < 2:
            return ("skip_err", race_id, None)
        race = {"date": TARGET_DATE, "venue": venue, "surface": surface,
                "distance": distance, "num_horses": len(horses),
                "track_cond": track_cond, "race_name": rname}
        pace_patterns, first_out = {}, None
        for label, cond in TRACK_PATTERNS:
            r2 = dict(race)
            r2["track_cond"] = cond
            try:
                out = predict_formation(conn, r2, horses, n_sim=N_SIM, seed=1)
            except Exception as e:
                return ("err", f"{race_id} {rname} ({label}): {e}", None)
            if out is None or out.get("excluded"):
                return ("skip_err", race_id, None)
            if first_out is None:
                first_out = out
            p = out["pace"]
            pace_patterns[label] = {
                "h": round(p["h_rate"], 3) if p["h_rate"] is not None else None,
                "m": round(p["m_rate"], 3) if p["m_rate"] is not None else None,
                "s": round(p["s_rate"], 3) if p["s_rate"] is not None else None,
                # 馬場パターン別の解説文(UIのpill切替と連動させる。無いと良・稍重の
                # 数値が他パターンでも表示されてしまう不整合が起きる)
                "comment": describe_pace(p, out["nige_count"], len(horses)),
            }
        if first_out is None:
            return ("skip_err", race_id, None)

        n = len(horses)
        c2 = first_out["c2"]
        horses_out = [{
            "num": h["umaban"],
            "waku": umaban_to_waku(h["umaban"], n),
            "name": h["horse_name"],
            "jockey": h["jockey"],
            "style": c2[i]["style"],
        } for i, h in enumerate(horses)]

        formation_out = {}
        gaps_out = {}
        for key, phase_label in STAGE_MAP:
            order = first_out["formation"].get(phase_label)
            if order is None:
                continue
            internal_key = first_out["phase_map"][phase_label]
            sd_list = first_out["rank_sd"].get(internal_key)
            tier_probs = first_out["tier_probs"].get(internal_key)
            gfl_list = first_out["gap_from_leader"].get(internal_key)
            surge_list = first_out["surge_p"] if key == "goal" else None
            formation_out[key] = [{"num": horses[i]["umaban"],
                                    "tier": rank_to_tier(rank, n),
                                    "sd": round(sd_list[i], 2) if sd_list else None,
                                    "tp": [round(x, 3) for x in tier_probs[i]] if tier_probs else None,
                                    "gfl": round(gfl_list[i], 2) if gfl_list else None,
                                    "surge": round(surge_list[i], 3) if surge_list else None}
                                   for rank, i in enumerate(order)]
            gb = first_out["gap_between"].get(internal_key)
            gaps_out[key] = [round(x, 2) for x in gb] if gb else None

        race_json = {
            "race_id": race_id, "venue": venue, "rno": rno, "rname": rname,
            "surface": surface, "distance": distance, "n_horses": n,
            "numbers_estimated": numbers_estimated,
            "nige_count": first_out["nige_count"],
            "pace": pace_patterns,
            "comment": describe_pace(first_out["pace"], first_out["nige_count"], n),
            "horses": horses_out,
            "formation": formation_out,
            "gaps_between": gaps_out,
            "churn_avg": round(first_out["churn_avg"], 3) if first_out["churn_avg"] is not None else None,
            "churn_label": first_out["churn_label"],
            "accuracy": get_accuracy_entry(venue, surface, distance),
        }
        return ("ok", f"{venue}{rno}R {rname} {surface}{distance}m {n}頭"
                      f"{' (馬番は推定)' if numbers_estimated else ''}", race_json)
    finally:
        conn.close()


def main():
    from multiprocessing import Pool
    workers = 14

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.execute("PRAGMA cache_size=-65536")
    day_races = fetch_day_races(conn)
    live_mode = False
    if not day_races:
        day_races = fetch_day_races_live()
        live_mode = bool(day_races)
        if live_mode:
            print(f"{TARGET_DATE}: resultsに未反映のためthis_week_races.jsonのライブ経路を使用")
    conn.close()
    print(f"{TARGET_DATE}: {len(day_races)}レース" + ("(ライブ)" if live_mode else ""))

    targets = []
    n_skip_shinba = 0
    for race_id, venue, rno, rname, surface, distance, n_ent, track_cond in day_races:
        if pace_cls_group(rname) == "新馬":
            n_skip_shinba += 1
            continue
        targets.append((race_id, venue, rno, rname, surface, distance, n_ent, track_cond, live_mode))

    races_out, n_skip_err = [], 0
    with Pool(workers) as pool:
        for status, msg, race_json in pool.imap_unordered(_process_one_race, targets, chunksize=1):
            if status == "ok":
                races_out.append(race_json)
                print(f"  OK {msg}")
            elif status == "err":
                print(f"  ERR {msg}")
                n_skip_err += 1
            else:
                n_skip_err += 1
    races_out.sort(key=lambda r: (r["venue"], r["rno"]))

    payload = {
        "date": TARGET_DATE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_sim": N_SIM,
        "note": "隊列予測は相関ρ≈0.45-0.5程度の参考情報です(mc_dyn展開シミュレーター)。"
                "買い目ロジック(MC)とは独立した予測です。",
        "races": races_out,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    for p in OUT_PATHS:
        if p.parent.exists():
            p.write_text(text, encoding="utf-8")
            print(f"書き出し: {p}")
    print(f"完了: {len(races_out)}R 出力 / 新馬スキップ{n_skip_shinba} / 対象外・失敗{n_skip_err}")


if __name__ == "__main__":
    main()
