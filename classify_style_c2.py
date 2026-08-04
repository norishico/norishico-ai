# -*- coding: utf-8 -*-
"""
classify_style_c2.py — C2a-ord 脚質分類の本番推論モジュール(2026-08-03採用)

generate_race_sim.classify_style() の後継候補としてレース単位で全馬の脚質を予測する。
classify_style()との違い:
  - レース単位で全馬まとめて予測する(レース文脈=他馬の予測脚質構成・レース内相対
    ランクを特徴量に使うため、1頭ずつでは計算できない)
  - ロジットモデル(TRAIN 2021-2023で学習・凍結)の連続「前寄り度」スコアを
    レース内で順位付けし、その順位を label_simple に射影してラベル化する
    (C2a-ord方式。クラス縮退が構造的に起きない)
  - 検証成績: VALID 2024-2025 acc 45.26%/macro 40.36%、2026HO acc 45.93%/macro 41.14%
    (現行classify_style: acc 33.72%/33.88%。常時追い込みベースライン: 39.67%/40.01%)

依存ファイル:
  - classify_style_c2_model.pkl (build_classify_style_c2_model.py で生成。
    ロジット3本 + 種牡馬/騎手prior + 設定値 + メタ情報)
  - keiba.db (過去走履歴の取得)
  - build_class_par.classify_class / L_MAP (クラス序数)
  - sklearn/numpy (モデルのunpickleと推論)

検証の出所: improve_classify_style_v2.py〜_v2d.py(4ラウンド、時系列分離済み)。
再学習頻度・担当: 未定(のりおさんが後日決定)。

主要API:
  classify_race_c2(conn, race, horses, model_path=None) -> list[dict]
    race   = {"date","surface","distance","num_horses","track_cond","race_name"}
    horses = [{"horse_name","jockey", 任意: "sire","umaban","weight_kg","horse_weight"}]
    戻り値 = 入力と同順の [{"horse_name","style","score","rank","n_hist","fallback"}]
             score=連続前寄り度(序数期待値0..3、小さいほど前)。同点時は入力順で安定。
"""
import math
import pickle
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

from build_class_par import classify_class, L_MAP

MODEL_PATH_DEFAULT = Path(__file__).parent / "classify_style_c2_model.pkl"
_MODEL_CACHE: dict = {}


def load_model(model_path=None) -> dict:
    path = str(model_path or MODEL_PATH_DEFAULT)
    if path not in _MODEL_CACHE:
        with open(path, "rb") as f:
            _MODEL_CACHE[path] = pickle.load(f)
    return _MODEL_CACHE[path]


# ──────────────────────────────────────────────
# 基礎関数(mc_dyn_engine.classify_style_simple / 検証コードと同一ロジック)
# ──────────────────────────────────────────────
def _label_simple(pos4, num_horses):
    if not pos4 or not num_horses or num_horses <= 1:
        return "先行"
    ratio = pos4 / num_horses
    if pos4 == 1 or ratio < 0.15:
        return "逃げ"
    elif ratio < 0.35:
        return "先行"
    elif ratio < 0.60:
        return "差し"
    else:
        return "追い込み"


def _wavg(values, decay):
    ws = [decay ** i for i in range(len(values))]
    return sum(v * w for v, w in zip(values, ws)) / sum(ws)


def _cond_grp(track_cond):
    if not track_cond:
        return None
    return 0 if str(track_cond).startswith("良") else 1


def _cls_level(race_name):
    cls = classify_class(race_name)
    return L_MAP.get(cls) if cls else None


# ──────────────────────────────────────────────
# 履歴取得(get_horse_history と同一の窓・フォールバック規則 + 拡張列)
# タプル形式: (date, surface, distance, pos4, nh, early, cond_grp, clsL, has_early,
#             jockey, horse_weight)  — 検証コード(improve_classify_style_v2c)と同一
# ──────────────────────────────────────────────
_HIST_SQL_SAME = """
    SELECT date, surface, distance, pos4, num_horses, pos1, pos2, pos3,
           track_cond, race_name, jockey, horse_weight
    FROM results
    WHERE TRIM(horse_name) = TRIM(?) AND date < ? AND date >= ?
      AND finish < 90 AND surface = ?
    ORDER BY date DESC LIMIT ?
"""
_HIST_SQL_ALL = """
    SELECT date, surface, distance, pos4, num_horses, pos1, pos2, pos3,
           track_cond, race_name, jockey, horse_weight
    FROM results
    WHERE TRIM(horse_name) = TRIM(?) AND date < ? AND date >= ?
      AND finish < 90
    ORDER BY date DESC LIMIT ?
"""


def _get_history_ext(conn, horse_name, current_date, surface, cfg):
    cutoff = (datetime.strptime(current_date, "%Y-%m-%d")
              - timedelta(days=cfg["hist_max_days"])).strftime("%Y-%m-%d")
    lim = cfg["hist_max_races"]
    rows = conn.execute(_HIST_SQL_SAME,
                        (horse_name, current_date, cutoff, surface, lim)).fetchall()
    if len(rows) < 5:
        rows = conn.execute(_HIST_SQL_ALL,
                            (horse_name, current_date, cutoff, lim)).fetchall()
    hist = []
    for (d, sf, dist, pos4, nh, p1, p2, p3, cond, rname, jockey, hw) in rows:
        early = p1 if (p1 and p1 > 0) else (p2 if (p2 and p2 > 0) else None)
        has_early = early is not None
        if early is None:
            early = p3 if (p3 and p3 > 0) else pos4
        hist.append((d, sf, dist, pos4, nh, early, _cond_grp(cond),
                     _cls_level(rname), has_early, jockey or "", hw))
    return hist


# ──────────────────────────────────────────────
# 特徴量(improve_classify_style_v2/v2b/v2c からの忠実移植)
# ──────────────────────────────────────────────
def _extract_features(history, target_distance, target_nh, surface):
    valid = [h for h in history if h[3] and h[4] and h[4] > 0]
    if not valid:
        return None
    ratios = [h[3] / h[4] for h in valid]
    n = len(valid)
    mean_r = sum(ratios) / n
    sd_r = (sum((r - mean_r) ** 2 for r in ratios) / n) ** 0.5 if n > 1 else 0.25
    dist_close = [r for h, r in zip(valid, ratios)
                  if h[2] and abs(h[2] - target_distance) <= 300]
    return {
        "n": n, "ratios": ratios, "mean_r": mean_r, "sd_r": sd_r,
        "last1_r": ratios[0], "last3_r": sum(ratios[:3]) / min(3, n),
        "eq1_rate": sum(1 for h in valid if h[3] == 1) / n,
        "dist_mean_r": (sum(dist_close) / len(dist_close)) if dist_close else mean_r,
        "n_dist": len(dist_close), "target_nh": target_nh,
        "is_dirt": 1.0 if surface == "ダ" else 0.0, "target_dist": target_distance,
        "valid": valid,
    }


def _build_ext(hist, feat, cond_grp, clsL):
    if feat is None:
        return None
    valid = feat["valid"]
    pos4_rs = feat["ratios"]
    early_rs = [h[5] / h[4] if h[5] else h[3] / h[4] for h in valid]
    deltas = [p - e for p, e in zip(pos4_rs, early_rs)]
    cond_match = [r for h, r in zip(valid, pos4_rs)
                  if cond_grp is not None and h[6] == cond_grp]
    hist_L = [h[7] for h in valid if h[7] is not None]
    return {
        "early_rs": early_rs, "deltas": deltas,
        "frac_early": sum(1 for h in valid if h[8]) / len(valid),
        "cond_match_rs": cond_match,
        "cls_rise": (clsL - (sum(hist_L) / len(hist_L)))
                    if (clsL is not None and hist_L) else 0.0,
    }


def _shrunk(prior, key, k, global_r):
    n, mean = prior.get(key, (0, global_r))
    return (n * mean + k * global_r) / (n + k), n


def _predict_v4(feat, decay):
    """M.predict_v4と同一(履歴あり馬の文脈用暫定脚質)"""
    nh_t = feat["target_nh"]
    score = defaultdict(float)
    proj = []
    for i, r in enumerate(feat["ratios"]):
        pos_t = max(1, round(r * nh_t))
        lab = _label_simple(pos_t, nh_t)
        proj.append(lab)
        score[lab] += decay ** i
    return max(score.items(), key=lambda kv: (kv[1], -proj.index(kv[0])))[0]


def _v3_vec(feat, decay):
    return [
        _wavg(feat["ratios"], decay), feat["last1_r"], feat["last3_r"],
        feat["sd_r"], feat["eq1_rate"], feat["dist_mean_r"],
        math.log(feat["n"] + 1), feat["target_nh"] / 18.0,
        feat["is_dirt"], feat["target_dist"] / 1000.0,
    ]


def _v5_vec(d, decay):
    f, ext = d["feat"], d["ext"]
    cond_rs = ext["cond_match_rs"]
    return _v3_vec(f, decay) + [
        _wavg(ext["early_rs"], decay), _wavg(ext["deltas"], decay),
        ext["frac_early"],
        (sum(cond_rs) / len(cond_rs)) if cond_rs else f["mean_r"],
        math.log1p(len(cond_rs)), ext["cls_rise"],
        d["sire_r"], math.log1p(d["sire_n"]),
        d["jockey_r"], math.log1p(d["jockey_n"]),
        float(d["cond_grp"] or 0),
    ]


def _fb_vec(d):
    return [d["sire_r"], math.log1p(d["sire_n"]), d["jockey_r"],
            math.log1p(d["jockey_n"]), d["nh"] / 18.0,
            d["distance"] / 1000.0, 1.0 if d["surface"] == "ダ" else 0.0,
            float(d["cond_grp"] or 0)]


def _ctx_vec(d):
    return [d["rank_pct"], d["rel_r"], d["others_mean_r"],
            min(d["n_others_nige"], 5) / 5.0, d["frac_others_front"],
            d["frac_others_low"]]


def _extras_vec(d):
    return [d["umaban_pct"], d["umaban_miss"], d["wkg_norm"],
            d["hw_delta"], d["hw_miss"], d["jm_frac"], d["jm_diff"]]


def _rhat(d, cfg):
    f = d["feat"]
    if f and f["n"] >= 1:
        return _wavg(f["ratios"], cfg["decay"])
    cap, k0, gr = cfg["rhat_cap_n"], cfg["rhat_base_k"], cfg["global_r"]
    wj, ws = min(d["jockey_n"], cap), min(d["sire_n"], cap)
    return (wj * d["jockey_r"] + ws * d["sire_r"] + k0 * gr) / (wj + ws + k0)


# ──────────────────────────────────────────────
# 本体
# ──────────────────────────────────────────────
def classify_race_c2(conn, race, horses, model_path=None):
    """レース単位でC2a-ord脚質予測を行う(モジュールdocstring参照)。"""
    import numpy as np
    mdl = load_model(model_path)
    cfg = mdl["config"]
    decay, gr = cfg["decay"], cfg["global_r"]
    sire_p, jockey_p = mdl["priors"]["sire"], mdl["priors"]["jockey"]
    ords = cfg["ords"]

    date_, surface = race["date"], race["surface"]
    # 【2026-08-04修正】raceのnum_horsesがDB不整合で0/NULLになっている実例を確認
    # (例: 2026-08-01_中京_1、実際は10頭出走なのにnum_horses=0)。horsesの実件数に
    # フォールバックしてumaban_pct計算等でのZeroDivisionErrorを防ぐ。
    distance, nh = race["distance"], race["num_horses"] or len(horses)
    cond_grp = _cond_grp(race.get("track_cond"))
    clsL = _cls_level(race.get("race_name"))

    ds = []
    for h in horses:
        hn = h["horse_name"]
        jockey = h.get("jockey") or ""
        sire = h.get("sire")
        if sire is None:
            row = conn.execute(
                "SELECT TRIM(sire) FROM results WHERE TRIM(horse_name)=TRIM(?) "
                "AND sire IS NOT NULL ORDER BY date DESC LIMIT 1", (hn,)).fetchone()
            sire = row[0] if row else ""
        hist = _get_history_ext(conn, hn, date_, surface, cfg)
        feat = _extract_features(hist, distance, nh, surface)
        ext = _build_ext(hist, feat, cond_grp, clsL)
        sr, sn = _shrunk(sire_p, sire or "", cfg["sire_shrink_k"], gr)
        jr, jn = _shrunk(jockey_p, (jockey, surface), cfg["jockey_shrink_k"], gr)
        valid = feat["valid"] if feat else []
        jm_frac = jm_diff = 0.0
        if valid and jockey:
            same = [h_[3] / h_[4] for h_ in valid if h_[9] == jockey]
            jm_frac = len(same) / len(valid)
            if same:
                jm_diff = (sum(same) / len(same)) - feat["mean_r"]
        hw = h.get("horse_weight")
        hw_delta, hw_miss = 0.0, 1.0
        if valid and hw and valid[0][10]:
            hw_delta, hw_miss = (hw - valid[0][10]) / 20.0, 0.0
        umaban = h.get("umaban")
        ds.append({
            "hn": hn, "feat": feat, "ext": ext, "cond_grp": cond_grp,
            "sire_r": sr, "sire_n": sn, "jockey_r": jr, "jockey_n": jn,
            "nh": nh, "distance": distance, "surface": surface,
            "umaban_pct": (umaban / nh) if umaban else 0.5,
            "umaban_miss": 0.0 if umaban else 1.0,
            "wkg_norm": ((h.get("weight_kg") or 55.0) - 55.0) / 3.0,
            "hw_delta": hw_delta, "hw_miss": hw_miss,
            "jm_frac": jm_frac, "jm_diff": jm_diff,
        })

    # レース文脈(全て事前情報: 各馬の過去走ベース暫定脚質とrhat)
    m = len(ds)
    hist_idx = [i for i, d in enumerate(ds) if d["feat"] and d["feat"]["n"] >= 1]
    fb_idx = [i for i, d in enumerate(ds) if d["feat"] is None or d["feat"]["n"] < 1]
    v4fb = [None] * m
    for i in hist_idx:
        v4fb[i] = _predict_v4(ds[i]["feat"], decay)
    if fb_idx:
        Xf = mdl["ctx_fb"]["scaler"].transform(
            np.array([_fb_vec(ds[i]) for i in fb_idx]))
        for i, p in zip(fb_idx, mdl["ctx_fb"]["clf"].predict(Xf)):
            v4fb[i] = p
    rhats = [_rhat(d, cfg) for d in ds]
    order0 = sorted(range(m), key=lambda i: rhats[i])
    rank0 = {i: k + 1 for k, i in enumerate(order0)}
    sum_r = sum(rhats)
    n_nige = sum(1 for p in v4fb if p == "逃げ")
    n_front = sum(1 for p in v4fb if p in ("逃げ", "先行"))
    n_low = sum(1 for r in rhats if r < 0.20)
    for i, d in enumerate(ds):
        others = m - 1
        d["rank_pct"] = rank0[i] / m
        d["others_mean_r"] = ((sum_r - rhats[i]) / others) if others else gr
        d["rel_r"] = rhats[i] - d["others_mean_r"]
        d["n_others_nige"] = n_nige - (1 if v4fb[i] == "逃げ" else 0)
        d["frac_others_front"] = ((n_front - (1 if v4fb[i] in ("逃げ", "先行") else 0))
                                  / others) if others else 0.0
        d["frac_others_low"] = ((n_low - (1 if rhats[i] < 0.20 else 0))
                                / others) if others else 0.0

    # 連続スコア(序数期待値) → レース内ランク射影
    pair = mdl["v6full"]
    scores = [0.0] * m
    if hist_idx:
        Xh = pair["sh"].transform(np.array([_v5_vec(ds[i], decay) + _ctx_vec(ds[i])
                                            + _extras_vec(ds[i]) for i in hist_idx]))
        proba = pair["ch"].predict_proba(Xh)
        mv = np.array([ords[c] for c in pair["ch"].classes_])
        for k, i in enumerate(hist_idx):
            scores[i] = float(proba[k] @ mv)
    if fb_idx:
        Xf = pair["sf"].transform(np.array([_fb_vec(ds[i]) + _ctx_vec(ds[i])
                                            + _extras_vec(ds[i]) for i in fb_idx]))
        proba = pair["cf"].predict_proba(Xf)
        mv = np.array([ords[c] for c in pair["cf"].classes_])
        for k, i in enumerate(fb_idx):
            scores[i] = float(proba[k] @ mv)

    out = [None] * m
    if m == 1:
        return [{"horse_name": ds[0]["hn"], "style": "先行", "score": scores[0],
                 "rank": 1, "n_hist": ds[0]["feat"]["n"] if ds[0]["feat"] else 0,
                 "fallback": 0 in fb_idx}]
    order = sorted(range(m), key=lambda i: scores[i])
    for k, i in enumerate(order):
        out[i] = {
            "horse_name": ds[i]["hn"],
            "style": _label_simple(k + 1, m),
            "score": scores[i],
            "rank": k + 1,
            "n_hist": ds[i]["feat"]["n"] if ds[i]["feat"] else 0,
            "fallback": i in fb_idx,
        }
    return out
