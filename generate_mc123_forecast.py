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
    reset_live_races_cache,
)
from build_class_par import build_class_par_table, calibrate_k_cls
from build_pace_baseline import build_baseline_table
from build_extra_par import build_rank_par, build_margin_par, build_l3f_par
from mc123_batch import load_horse_hist_all, load_same_day_bias_dict, precompute_horse_features_fast
from mc123_engine import run_mc123, hash64_seed
from generate_race_sim import classify_style_c2
from mc_dyn_engine import is_jump_race

DB = "keiba.db"


def _pop_opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        v = args[i + 1]
        del args[i:i + 2]
        return v
    return default


# 日付は必ずargv[1]に置く(generate_pace_forecast側もsys.argv[1]を日付として読むため)。
# オプションは日付の後ろ: --out PATH(mc123_data.json出力先の退避、テスト用) /
# --forward-dir DIR(Forward検証ログ保存先) / --log-finished(終了済みレースも記録、テスト専用)
_ARGS = sys.argv[1:]
_OUT_OVERRIDE = _pop_opt(_ARGS, "--out")
FORWARD_DIR = Path(_pop_opt(_ARGS, "--forward-dir", "mc123_forward"))
LOG_FINISHED = "--log-finished" in _ARGS
if LOG_FINISHED:
    _ARGS.remove("--log-finished")
OUT_PATHS = [Path(_OUT_OVERRIDE) if _OUT_OVERRIDE else Path("mc_keiba_public/mc123_data.json")]
TARGET_DATE = _ARGS[0] if _ARGS else _date.today().isoformat()
N_MC = 10000
MIN_DELIVERY_RATIO = 0.8  # 対象レースのうち出力できた割合がこれ未満なら失敗扱い(2026-11-06新設)
TRACK_PATTERNS = [("良・稍重", "良"), ("重", "重"), ("不良", "不")]

# 2026-09-21新設: AYOkeiba「注目レース」タブのオッズ取得ボタン用にnk_id(netkeiba race_id、
# 12桁)を各レースへ付与する。場コードはjvlink_fetch.JYO_NAMEの値をそのまま複製したもの
# (jvlink_fetch.py自体は32bit Python専用でwin32com.clientをimportするため、64bit側の
# 本スクリプトからは直接importできない)。
_VENUE_TO_CODE = {
    "札幌": "01", "函館": "02", "福島": "03", "新潟": "04", "東京": "05",
    "中山": "06", "中京": "07", "京都": "08", "阪神": "09", "小倉": "10",
}

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


def load_nk_id_map():
    """this_week_races.jsonからvenue×race_num→nk_id(netkeiba race_id)のマップを返す。
    generate_mc_record.load_start_time_mapと同じロジック(venue名+race_numでの突合)。"""
    p = Path("this_week_races.json")
    if not p.exists():
        return {}
    races = json.loads(p.read_text(encoding="utf-8"))
    return {(r.get("venue", ""), r.get("race_num", 0)): r.get("race_id", "")
            for r in races if r.get("date") == TARGET_DATE and r.get("race_id")}


def resolve_nk_id(conn, nk_id_map, race_id, venue, rno):
    """レースのnk_id(netkeiba race_id)を解決する。取得順:
    1. this_week_races.json(venue+race_numで突合、load_nk_id_map())
    2. results.kai/results.week_numから構築: {年}{場コード}{kai}{week_num}{race_num}
    3. どちらも不可ならNone(フロント側はオッズ取得ボタンを非表示にする)"""
    nk = nk_id_map.get((venue, rno))
    if nk:
        return nk
    code = _VENUE_TO_CODE.get(venue)
    if not code:
        return None
    row = conn.execute(
        "SELECT kai, week_num FROM results WHERE race_id = ? LIMIT 1", (race_id,)
    ).fetchone()
    if not row or row[0] is None or row[1] is None:
        row = conn.execute(
            "SELECT kai, week_num FROM results WHERE date = ? AND venue = ? "
            "AND kai IS NOT NULL AND week_num IS NOT NULL LIMIT 1",
            (TARGET_DATE, venue),
        ).fetchone()
    if not row or row[0] is None or row[1] is None:
        return None
    kai, week_num = row
    try:
        year = int(TARGET_DATE[:4])
        return f"{year}{code}{int(kai):02d}{int(week_num):02d}{int(rno):02d}"
    except (ValueError, TypeError):
        return None


def umaban_to_gate(umaban):
    if not umaban or umaban <= 0:
        return 4
    return min((umaban + 1) // 2, 8)


def fetch_horses_for_mc123(conn, race_id, live_mode):
    """MC123用のhorses(horse_name/umaban/jockey/gate/style/finish)を組み立てる。

    weight_kg(斤量)はclassify_style_c2のwkg_norm特徴量に使われる。2026-08-15発見:
    この関数がweight_kgを渡していなかったため、classify_style_c2側で全馬が既定値55.0kg
    (平均斤量)扱いになり、実斤量を渡している展開予想タブ(generate_pace_forecast.py)側と
    脚質判定が食い違うケースがあった(斤量が55kgから離れた僅差の馬で脚質ラベルが入れ替わる)。
    """
    # 2026-09-05修正: 従来はumaban欠損時に無警告で出走順連番へフォールバックしており、
    # generate_pace_forecast.py側で対策済みの「this_week_races.json非アトミック書き換えの
    # 狭間読み→馬番と馬名の対応がズレる」不具合(2026-08-23、中京1R等で発覚)への
    # ガードがmc123側だけ欠けていた。numbers_estimatedを返すようにし、呼び出し側で
    # pace側と同じリトライ・スキップ判断ができるようにする。
    # 2026-09-20 F1修正: 取消・除外馬を除外する。ライブ経路はfetch_shutsuba.py/
    # fetch_shutsuba_sp.pyが付与するscratchedフラグで判定(weekend_predictions.jsonは
    # this_week_races.jsonのhorsesをそのまま透過するため、この時点でflag済み)。
    # DB経路はレースが1頭でも確定済み(finish NOT NULL)なら、そのレース自体は既に
    # 終わっているとみなし、finish NULLの残り行は「未確定(これから出走)」ではなく
    # 「取消馬」として除外する(旧ロジックは両者を区別できず取消馬を出走馬として
    # 予想に混ぜてしまっていた。2026-09-20中山3R オオルリで実際に発生を確認)。
    if live_mode:
        live = _load_live_races()
        r = live.get(race_id)
        if r is None:
            return [], False, 0
        rows = r.get("horses", [])
        out = []
        numbers_estimated = False
        n_scratched = 0
        for hi, h in enumerate(rows):
            if h.get("scratched"):
                n_scratched += 1
                continue
            uma = h.get("umaban")
            if not uma:
                uma = hi + 1
                numbers_estimated = True
            try:
                wkg = float(h.get("weight", "") or "")
            except ValueError:
                wkg = None
            out.append({"horse_name": (h.get("name") or "").strip(), "umaban": uma,
                        "jockey": (h.get("jockey") or "").strip(), "gate": umaban_to_gate(uma),
                        "weight_kg": wkg, "style": None, "finish": None})
        return out, numbers_estimated, n_scratched
    has_confirmed = conn.execute(
        "SELECT 1 FROM results WHERE race_id = ? AND finish IS NOT NULL LIMIT 1", (race_id,)
    ).fetchone() is not None
    if has_confirmed:
        finish_filter = "finish IS NOT NULL AND finish < 90"
        n_scratched = conn.execute(
            "SELECT COUNT(*) FROM results WHERE race_id = ? AND finish IS NULL", (race_id,)
        ).fetchone()[0]
    else:
        finish_filter = "finish IS NULL OR finish < 90"
        n_scratched = 0
    rows = conn.execute(f"""
        SELECT TRIM(horse_name), jockey, umaban, weight_kg
        FROM results WHERE race_id = ? AND ({finish_filter})
        ORDER BY (umaban IS NULL), umaban, horse_name
    """, (race_id,)).fetchall()
    out = []
    numbers_estimated = False
    for hi, r in enumerate(rows):
        uma = r[2]
        if not uma:
            uma = hi + 1
            numbers_estimated = True
        out.append({"horse_name": r[0], "umaban": uma, "jockey": r[1] or "",
                    "gate": umaban_to_gate(uma), "weight_kg": r[3], "style": None, "finish": None})
    return out, numbers_estimated, n_scratched


def _fetch_horses_with_retry(conn, race_id, live_mode, max_retry=3, wait_sec=5):
    """generate_pace_forecast.py側の三重ガード(リトライ・スキップ・numbers_estimated)を移植。
    live_modeでumaban欠損が発生するのは、auto_refresh.pyがthis_week_races.jsonを非アトミックに
    書き換え続けている狭間を読んでしまった異常事態のサインであり、そのまま出すと馬番と馬名が
    食い違った誤ったデータを公開してしまう(2026-08-23、中京1R等でpace側にて実際に発生)。"""
    for attempt in range(max_retry + 1):
        horses, est, n_scratched = fetch_horses_for_mc123(conn, race_id, live_mode)
        if not live_mode or not est or not horses:
            return horses, est, n_scratched
        if attempt < max_retry:
            print(f"  ⚠ {race_id}: 馬番が推定値(欠損スナップショットの疑い)、"
                  f"{wait_sec}秒待って読み直します(試行{attempt+1}/{max_retry})")
            time.sleep(wait_sec)
            reset_live_races_cache()
    return horses, est, n_scratched


def detail_by_umaban(det, horses):
    """run_mc123(return_detail=True)のdetail(horsesのindex基準)を、馬番(umaban)基準に変換する。
    Forward検証ログ専用(2026-11-07新設)。サイトのmc123_data.jsonには出さない(非表示で計測のみ)。"""
    um = [int(h["umaban"]) for h in horses]
    conv = lambda rows: [[um[i] for i in r[:-1]] + [r[-1]] for r in rows]
    return {
        "pace_probs": det["pace_probs"], "n_pace": det["n_pace"],
        "cond": {P: (None if c is None else {str(um[i]): x for i, x in enumerate(c)})
                 for P, c in det["cond"].items()},
        "umaren": conv(det["umaren"]), "wide": conv(det["wide"]), "sanrenpuku": conv(det["sanrenpuku"]),
    }


def validate_race_output(horses, patterns):
    """出力直前の整合性検証(2026-11-06新設)。エラー文字列のリストを返し、空なら正常。
    weekend_predictions.jsonの78%サイレント欠落(2026-07-22)や、margin文字列のTypeErrorのように
    「壊れたデータが黙って出る/黙って欠ける」事故を、生成時点でfail-loudに検知するためのガード。
    確率の合計は、全シミュレーションで必ず1頭が1着/2着/3着になるため p1/p2/p3=1、
    ptop3=p1+p2+p3=3(n>=3)になる(4桁丸め誤差のみ許容)。"""
    errs = []
    n = len(horses)
    if not patterns:
        return ["馬場パターンの結果が1つもない"]
    names = [h["horse_name"] for h in horses]
    if any(not x for x in names):
        errs.append("馬名が空")
    if len(set(names)) != n:
        errs.append("馬名が重複")
    try:
        umas = [int(h["umaban"]) for h in horses]
    except (TypeError, ValueError):
        umas = []
        errs.append("馬番が数値でない")
    if umas and (len(set(umas)) != n or min(umas) <= 0):
        errs.append("馬番が重複/不正")
    targets = (("p1", 1.0), ("p2", 1.0), ("p3", 1.0), ("ptop3", float(min(3, n))))
    for label, rows in patterns.items():
        if len(rows) != n:
            errs.append(f"{label}: 行数{len(rows)}!=頭数{n}")
            continue
        for key, target in targets:
            vals = [r[key] for r in rows]
            if any(v is None or v != v or v < 0 or v > 1 for v in vals):
                errs.append(f"{label}: {key}にNaN/範囲外")
                continue
            s = sum(vals)
            if abs(s - target) > 0.02:
                errs.append(f"{label}: sum({key})={s:.4f} (期待{target})")
    return errs


def _engine_tag():
    """mc123_engine.pyのハッシュ先頭8桁。Forward検証ログに「どのエンジン版の予測か」を残す。"""
    import hashlib
    p = Path(__file__).resolve().parent / "mc123_engine.py"
    return hashlib.sha1(p.read_bytes()).hexdigest()[:8]


def log_forward(race_meta, horses, patterns, n_mc, generated_at, engine_tag, details=None):
    """発走前の予測をForward検証ログに保存する(2026-11-06新設)。

    LOCK規則: 同一race_idは先着保存(2回目以降の実行では上書きしない)。後から再実行して
    結果を見た上で予測を差し替える余地を構造的に無くす。ログは日付別JSON(git追跡可能な
    テキスト、*.dbは.gitignore対象のため不採用)。原子的書き込み(tmp→os.replace)。
    戻り値: 新規保存したらTrue、既存でスキップしたらFalse。"""
    import os
    FORWARD_DIR.mkdir(parents=True, exist_ok=True)
    path = FORWARD_DIR / f"{TARGET_DATE}.json"
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"date": TARGET_DATE, "races": {}}
    if race_meta["race_id"] in doc["races"]:
        return False
    doc["races"][race_meta["race_id"]] = {
        **race_meta, "generated_at": generated_at, "n_mc": n_mc, "engine_tag": engine_tag,
        "horses": [{"umaban": int(h["umaban"]), "name": h["horse_name"]} for h in horses],
        "patterns": {label: [{"p1": r["p1"], "p2": r["p2"], "p3": r["p3"], "ptop3": r["ptop3"]}
                             for r in rows]
                     for label, rows in patterns.items()},
    }
    if details:
        # 非表示の計測用(2026-11-07新設): ペース別の条件付き確率・pH/pM/pS・組合せ確率の上位
        doc["races"][race_meta["race_id"]]["detail"] = details
    tmp = path.with_suffix(".json.tmp")
    # 詳細つきで1日あたりのサイズが増えるためコンパクト形式で保存(評価側は形式非依存)
    tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
    return True


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    # 2026-09-20修正: 従来は「resultsに1件でも(umaban完備の)レースがあれば当日全部を
    # resultsのみで処理」という二択だったため、同日の一部レースだけ終了しumaban確定済み・
    # 残りは未確定(=これから行われる、予想として最も価値がある側)という状態だと、未確定側が
    # 丸ごと欠落していた(fetch_saturday_results.pyのumaban遅延バグ修正の副作用で表面化)。
    # results側(umaban完備)とライブ側(this_week_races.json)をrace_id単位でマージし、
    # 各レースごとにどちらの経路から来たかを個別に記憶して使い分ける。
    results_races = fetch_day_races(conn)
    results_ids = {r[0] for r in results_races}
    live_races = fetch_day_races_live()
    extra_live = [r for r in live_races if r[0] not in results_ids]
    day_races = sorted(list(results_races) + extra_live, key=lambda x: (x[1], x[2]))
    live_mode_by_id = {r[0]: False for r in results_races}
    live_mode_by_id.update({r[0]: True for r in extra_live})
    if extra_live:
        print(f"{TARGET_DATE}: 未終了{len(extra_live)}レースはthis_week_races.jsonのライブ経路を併用")
    print(f"{TARGET_DATE}: {len(day_races)}レース"
          + (f"(うちライブ{len(extra_live)})" if extra_live else ""))

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

    nk_id_map = load_nk_id_map()

    races_out, n_skip_shinba, n_skip_jump, n_skip_err, n_skip_umaban = [], 0, 0, 0, 0
    n_invalid, n_logged, n_log_skipped_finished = 0, 0, 0
    invalid_detail = []
    gen_at = datetime.now().isoformat(timespec="seconds")
    eng_tag = _engine_tag()
    for race_id, venue, rno, rname, surface, distance, n_ent, track_cond in day_races:
        if pace_cls_group(rname) == "新馬":
            n_skip_shinba += 1
            continue
        # 2026-09-20 F2修正: '障害' in rname単独だとJV-Link由来の切り詰めで漏れる(35レース
        # 実例確認済み)ため、is_jump_race()(mc_dyn_engine.py、surface/distance併用)に統一
        if is_jump_race(rname, surface, distance) or surface not in ("芝", "ダ"):
            n_skip_jump += 1
            continue
        horses, numbers_estimated, n_scratched = _fetch_horses_with_retry(conn, race_id, live_mode_by_id[race_id])
        if len(horses) < 3:
            n_skip_err += 1
            continue
        if live_mode_by_id[race_id] and numbers_estimated:
            n_skip_umaban += 1
            print(f"  SKIP {venue}{rno}R {rname}(リトライ後も馬番欠損、誤表示防止のため今回は見送り)")
            continue

        # 脚質(表示用+MC123内部のn_nige/n_front算出に必須。展開予想タブと同じclassify_style_c2)。
        # 2026-08-15: weight_kg(斤量)を含め忘れていたため、fetch_horses_for_mc123側で
        # 修正した後もここで再度落ちてしまい、展開予想タブと脚質判定が食い違う原因になっていた。
        pf_horses = [{"horse_name": h["horse_name"], "jockey": h["jockey"], "umaban": h["umaban"],
                      "weight_kg": h.get("weight_kg")}
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
        patterns, first_result, details = {}, None, {}
        for label, cond in TRACK_PATTERNS:
            race_info = {"venue": venue, "distance": distance, "track_cond": cond,
                        "num_horses": n, "date": TARGET_DATE, "surface": surface, "race_id": race_id}
            try:
                precompute_horse_features_fast(horses, race_info, horse_hist, class_par, k_cls,
                                                bias_map, pace_baseline, rank_par=rank_par,
                                                margin_par=margin_par, l3f_par=l3f_par)
                seed = hash64_seed(f"{race_id}_{cond}")
                result, det = run_mc123(horses, race_info, n_mc=N_MC, seed=seed, wind=None,
                                        return_detail=True)
            except Exception as e:
                print(f"  ERR {race_id} {rname} ({label}): {e}")
                result = None
            if result is None:
                continue
            if first_result is None:
                first_result = result
            if det is not None:
                details[label] = detail_by_umaban(det, horses)
            patterns[label] = [{"p1": round(r["p1"], 4), "p2": round(r["p2"], 4),
                               "p3": round(r["p3"], 4), "ptop3": round(r["ptop3"], 4)}
                              for r in result]
        if first_result is None:
            n_skip_err += 1
            continue

        # 2026-11-06新設: 出力直前の整合性検証。不正なレースは公開せず落とし、最後に失敗終了する
        errs = validate_race_output(horses, patterns)
        if errs:
            n_invalid += 1
            invalid_detail.append({"race_id": race_id, "errors": errs})
            print(f"  INVALID {venue}{rno}R {rname}: {'; '.join(errs)}")
            continue

        # 2026-09-20 F8修正: K_LOWINFO(低情報馬補正)は馬名の完全一致で履歴照合するため、
        # 表記ゆれで誤って「低情報馬」と判定される場合、レース単位で比率が異常に高くなる
        # はず。precompute_horse_features_fast()がhorses各要素に立てるlow_infoフラグを
        # 集計し、40%を超えたら警告ログを出す(今日時点の実測では誤検知ゼロを確認済みだが、
        # 将来のデータ品質劣化を検知するための予防ガード)。
        n_low_info = sum(1 for h in horses if h.get("low_info"))
        low_info_ratio = round(n_low_info / n, 3) if n else 0.0
        if low_info_ratio > 0.4:
            print(f"  ⚠ {venue}{rno}R {rname}: low_info_ratio={low_info_ratio:.0%} "
                  f"({n_low_info}/{n}頭) — 馬名照合の表記ゆれ等の可能性、要確認")

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
            "numbers_estimated": numbers_estimated, "n_scratched": n_scratched,
            "low_info_ratio": low_info_ratio,
            "horses": horses_out,
            "top1_reliability": get_top1_reliability_entry(venue, surface, distance),
            "nk_id": resolve_nk_id(conn, nk_id_map, race_id, venue, rno),
        })
        print(f"  OK {venue}{rno}R {rname} {surface}{distance}m {n}頭"
              f"{' (馬番は推定)' if numbers_estimated else ''}")

        # Forward検証ログ(2026-11-06新設): 発走前の予測のみ記録する。終了済みレース(finish確定)
        # は結果を見た後の予測になり検証として無効なので記録しない(--log-finishedはテスト専用)。
        # 馬番が推定値のレースも、馬番と馬名の対応が信頼できないため記録しない。
        finished = conn.execute(
            "SELECT 1 FROM results WHERE race_id = ? AND finish IS NOT NULL AND finish < 90 LIMIT 1",
            (race_id,)).fetchone() is not None
        if numbers_estimated or (finished and not LOG_FINISHED):
            n_log_skipped_finished += 1
        else:
            meta = {"race_id": race_id, "race_date": TARGET_DATE, "venue": venue, "rno": rno,
                    "rname": rname, "surface": surface, "distance": distance, "n_horses": n}
            if log_forward(meta, horses, patterns, N_MC, gen_at, eng_tag, details):
                n_logged += 1

    # 2026-11-06新設: 出力の完全性検証。対象レース数(新馬・障害を除く)に対する出力割合を見る
    expected = len(day_races) - n_skip_shinba - n_skip_jump
    delivered = len(races_out)
    fatal = []
    if expected > 0 and delivered / expected < MIN_DELIVERY_RATIO:
        fatal.append(f"出力{delivered}R / 対象{expected}R = {delivered/expected:.0%} "
                     f"(閾値{MIN_DELIVERY_RATIO:.0%}未満)")
    if n_invalid:
        fatal.append(f"整合性検証エラー{n_invalid}レース")

    # pace_data.jsonとのrace_id突合(同日・同基準のはずなので差分は要確認)
    pace_diff = None
    try:
        pace_path = Path("mc_keiba_public/pace_data.json")
        if pace_path.exists():
            pd_ = json.loads(pace_path.read_text(encoding="utf-8"))
            if pd_.get("date") == TARGET_DATE:
                pace_ids = {r["race_id"] for r in pd_["races"]}
                mc_ids = {r["race_id"] for r in races_out}
                pace_diff = {"only_pace": sorted(pace_ids - mc_ids), "only_mc123": sorted(mc_ids - pace_ids)}
                if pace_diff["only_pace"] or pace_diff["only_mc123"]:
                    print(f"  ⚠ pace_data.jsonとrace_id不一致: pace側のみ{len(pace_diff['only_pace'])} / "
                          f"MC123側のみ{len(pace_diff['only_mc123'])}")
    except Exception as e:
        print(f"  ⚠ pace_data.json突合に失敗(無視): {e}")

    payload = {
        "date": TARGET_DATE,
        "generated_at": gen_at,
        "n_mc": N_MC,
        "note": "MC123エンジンによる各馬の1〜3着内確率です。買い目(推奨馬券)ではなく参考情報です。"
                "オッズ(期待値)は考慮していません。",
        "validation": {"expected": expected, "delivered": delivered, "invalid": invalid_detail,
                       "pace_diff": pace_diff},
        "races": races_out,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    for p in OUT_PATHS:
        if not p.parent.exists():
            continue
        # 同日データを縮めて上書きしない(後発の不調な実行が、先行の良好な出力を潰すのを防ぐ)
        if p.exists():
            try:
                old = json.loads(p.read_text(encoding="utf-8"))
                if old.get("date") == TARGET_DATE and len(old.get("races", [])) > delivered:
                    fatal.append(f"{p}: 既存{len(old['races'])}R > 今回{delivered}R のため上書きしない")
                    continue
            except Exception:
                pass  # 既存が壊れているなら上書きで復旧する
        p.write_text(text, encoding="utf-8")
        print(f"書き出し: {p}")
    print(f"完了: {delivered}R 出力 / 新馬スキップ{n_skip_shinba} / 障害等スキップ{n_skip_jump} / "
          f"馬番欠損スキップ{n_skip_umaban} / 対象外・失敗{n_skip_err} / 検証エラー{n_invalid}")
    print(f"Forward検証ログ: 新規{n_logged}R保存 / 終了済み・馬番推定でスキップ{n_log_skipped_finished}R "
          f"(engine={eng_tag}, dir={FORWARD_DIR})")
    conn.close()
    if fatal:
        print("🚨 FAIL: " + " / ".join(fatal))
        sys.exit(1)


if __name__ == "__main__":
    main()
