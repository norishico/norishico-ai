"""
mc123_batch.py — mc123_engine.py の特徴量計算をDBクエリ0回の一括ロード版に書き換え。

Phase2実測: precompute_horse_features()が1レース(16頭)で5.83秒 → 全期間BTだと
非現実的な時間になる(コーディネーター試算: 16,500レース×5.8秒=約27時間)。
sim_bt_full.py: load_horse_hist()と同じ「全期間を1回だけ一括ロード→辞書引きのみ」
パターンに倣い、CTA(build_class_par)・PGR(build_pace_baseline)・pace_response
(classify_style + get_horse_pace_scores相当)を全てDBクエリなしの純粋関数に書き換える。
"""
import time
from collections import defaultdict

import generate_race_sim as gsim
from generate_race_sim import STYLE_DEFAULTS
from build_class_par import classify_class, L_MAP, RECENCY_WEIGHTS as CTA_WEIGHTS
from build_pace_baseline import (
    rel_finish, pos4_bucket, num_horses_bucket, RECENCY_WEIGHTS as GRIT_WEIGHTS,
    GRIT_CAP, MIN_N_GRIT,
)

HIST_START = "2015-01-01"


def load_horse_hist_all(conn, hist_start=HIST_START):
    """全期間の results x race_laps を1回だけ一括ロードし、horse_name -> [entry,...]
    (date昇順)の辞書を返す。CTA/PGR/pace_response すべてがこの1つの辞書だけで完結する。
    """
    rows = conn.execute("""
        SELECT r.horse_name, r.date, r.venue, r.surface, r.distance, r.finish,
               r.num_horses, r.pos4, r.last3f, r.time_sec, r.track_cond, r.race_id,
               r.race_name, rl.pace_type
        FROM results r
        LEFT JOIN race_laps rl ON rl.race_id = r.race_id
        WHERE r.date >= ? AND r.finish < 90
    """, (hist_start,)).fetchall()

    hist = defaultdict(list)
    for r in rows:
        entry = {
            "horse_name": r[0], "date": r[1], "venue": r[2], "surface": r[3],
            "distance": r[4], "finish": r[5], "num_horses": r[6], "pos4": r[7],
            "last3f": r[8], "time_sec": r[9], "track_cond": r[10], "race_id": r[11],
            "race_name": r[12] or "", "pace_type": r[13],
        }
        hist[r[0]].append(entry)

    for name in hist:
        hist[name].sort(key=lambda e: e["date"])  # 昇順

    return dict(hist)


def load_class_par_dict(conn):
    d = {}
    for cls, venue, surface, distance, track_cond, mu, sigma, n in conn.execute(
        "SELECT cls, venue, surface, distance, track_cond, mu, sigma, n FROM class_par"
    ):
        d[(cls, venue, surface, distance, track_cond)] = (mu, sigma, n)
    return d


def load_k_cls_dict(conn):
    return dict(conn.execute("SELECT surface, k_cls FROM class_k").fetchall())


def load_same_day_bias_dict(conn):
    d = {}
    for date, venue, surface, bias in conn.execute(
        "SELECT DISTINCT date, venue, surface, same_day_bias FROM race_level_index"
    ):
        d[(date, venue, surface)] = bias
    return d


def load_pace_baseline_dict(conn):
    d = {}
    for pb, pt, surf, nb, mean_rf, n in conn.execute(
        "SELECT pos4_bucket, pace_type, surface, nh_bucket, mean_rel_finish, n FROM pace_baseline"
    ):
        d[(pb, pt, surf, nb)] = (mean_rf, n)
    return d


# ── in-memory版 CTA ─────────────────────────────────────────
def _a_run_from_entry(e, class_par, k_cls, bias_map):
    cls = classify_class(e["race_name"])
    if cls is None or cls not in L_MAP or not e["track_cond"] or not e["time_sec"] or e["time_sec"] <= 0:
        return None
    key = (cls, e["venue"], e["surface"], e["distance"], e["track_cond"])
    cp = class_par.get(key)
    if cp is None:
        return None
    mu, sigma, n = cp
    if sigma <= 0:
        return None
    z_run = (e["time_sec"] - mu) / sigma
    bias = bias_map.get((e["date"], e["venue"], e["surface"]), 0.0)
    k = k_cls.get(e["surface"], 0.3)
    return L_MAP[cls] * k - (z_run - bias)


def compute_cta_fast(hist_list, asof_date, class_par, k_cls, bias_map, n_recent=5):
    """hist_list: 1頭分のentryリスト(date昇順)。asof_dateより前のものだけ使う。"""
    past = [e for e in hist_list if e["date"] < asof_date]
    recent = past[-n_recent:][::-1]  # 直近n_recent件、新しい順
    a_runs = []
    for e in recent:
        a = _a_run_from_entry(e, class_par, k_cls, bias_map)
        if a is not None:
            a_runs.append((e["date"], a))
    if not a_runs:
        return None, None, 0
    weights = CTA_WEIGHTS[:len(a_runs)]
    cta_full = sum(a * w for (_, a), w in zip(a_runs, weights)) / sum(weights)
    idx_sorted = sorted(range(len(a_runs)), key=lambda i: -a_runs[i][1])
    top2 = idx_sorted[:2]
    w_sum = sum(weights[i] for i in top2)
    cta_main = sum(a_runs[i][1] * weights[i] for i in top2) / w_sum
    return round(cta_main, 4), round(cta_full, 4), len(a_runs)


# ── in-memory版 PGR ─────────────────────────────────────────
_H_BUCKETS = {"前", "好位"}
_S_BUCKETS = {"中団", "後方"}


def compute_pgr_fast(hist_list, asof_date, baseline, lookback=20):
    past = [e for e in hist_list if e["date"] < asof_date and e["pace_type"] in ("H", "M", "S")]
    recent = past[-lookback:][::-1]  # 新しい順

    h_resids, s_resids = [], []
    for e in recent:
        pb = pos4_bucket(e["pos4"], e["num_horses"])
        nb = num_horses_bucket(e["num_horses"])
        rf = rel_finish(e["finish"], e["num_horses"])
        if pb is None or nb is None or rf is None or e["surface"] not in ("芝", "ダ"):
            continue
        key = (pb, e["pace_type"], e["surface"], nb)
        bl = baseline.get(key)
        if bl is None:
            continue
        mean_rf, n = bl
        resid = mean_rf - rf
        if e["pace_type"] == "H" and pb in _H_BUCKETS:
            h_resids.append(resid)
        elif e["pace_type"] == "S" and pb in _S_BUCKETS:
            s_resids.append(resid)

    def weighted_capped(resids):
        n = len(resids)
        if n < MIN_N_GRIT:
            return 0.0
        top = resids[:5]
        weights = GRIT_WEIGHTS[:len(top)]
        val = sum(r * w for r, w in zip(top, weights)) / sum(weights)
        return max(-GRIT_CAP, min(GRIT_CAP, val))

    return weighted_capped(h_resids), weighted_capped(s_resids)


# ── in-memory版 pace_response (classify_style + get_horse_pace_scores相当) ──
NEUTRAL_PACE_SCORE = 60.0
MIN_SAMPLES_PACE = 2


def _get_horse_pace_scores_fast(hist_list, asof_date, surface):
    past = [e for e in hist_list
            if e["date"] < asof_date and e["surface"] == surface
            and e["pace_type"] in ("H", "M", "S")]
    recent = past[-20:][::-1]

    pace_fin = {"H": [], "M": [], "S": []}
    pace_nh = {"H": [], "M": [], "S": []}
    for e in recent:
        pt, fin, nh = e["pace_type"], e["finish"], e["num_horses"]
        if fin and nh and nh > 0:
            pace_fin[pt].append(fin)
            pace_nh[pt].append(nh)

    result = {}
    for k in ("H", "M", "S"):
        fins, nhs = pace_fin[k], pace_nh[k]
        if len(fins) < MIN_SAMPLES_PACE:
            result[k] = NEUTRAL_PACE_SCORE
        else:
            scores = []
            for fin, nh in zip(fins, nhs):
                rel = 1.0 - (fin - 1) / (nh - 1) if nh > 1 else 0.5
                scores.append(rel * 100.0)
            result[k] = round(sum(scores) / len(scores), 1)
    return result


def compute_pace_response_fast(hist_list, asof_date, target_distance, surface, jockey=""):
    """classify_style()自体はDBを叩かない純粋関数なので、そのまま再利用する
    (hist_listをsim_bt_full.py互換のdict形式で渡す)。"""
    past = [e for e in hist_list if e["date"] < asof_date]
    # classify_styleは新しい順を期待(sim_bt_full.get_horse_history_fast参照)
    gsim_hist = [
        {"date": e["date"], "venue": e["venue"], "surface": e["surface"],
         "distance": e["distance"], "finish": e["finish"], "num_horses": e["num_horses"],
         "pos4": e["pos4"], "last3f": e["last3f"], "time_sec": e["time_sec"],
         "track_cond": e["track_cond"], "race_id": e["race_id"],
         "pace_type": e["pace_type"], "race_name": e["race_name"]}
        for e in past[-10:][::-1]
    ]
    style = gsim.classify_style(gsim_hist, target_distance, jockey=jockey, surface=surface)
    style_default = STYLE_DEFAULTS.get(style, STYLE_DEFAULTS["先行"])
    horse_pace = _get_horse_pace_scores_fast(hist_list, asof_date, surface)

    v = {p: round(0.6 * style_default[p] + 0.4 * horse_pace[p], 2) for p in ("H", "M", "S")}
    return style, v


def precompute_horse_features_fast(horses, race_info, horse_hist, class_par, k_cls,
                                    bias_map, pace_baseline):
    """mc123_engine.precompute_horse_features()のDBクエリ0回版。horse_histは
    load_horse_hist_all()の出力(horse_name -> [entry,...])。"""
    asof_date = race_info.get("date")
    distance = race_info.get("distance", 1600)
    surface = race_info.get("surface", "芝")

    for h in horses:
        hist_list = horse_hist.get(h["horse_name"], [])

        cta_main, cta_full, n_runs = compute_cta_fast(hist_list, asof_date, class_par, k_cls, bias_map)
        h["cta_z"] = cta_main if cta_main is not None else 0.0

        grit_h, grit_s = compute_pgr_fast(hist_list, asof_date, pace_baseline)
        h["grit_h"] = grit_h
        h["grit_s"] = grit_s

        style, v = compute_pace_response_fast(hist_list, asof_date, distance, surface,
                                               jockey=h.get("jockey", ""))
        h["pace_v"] = v
        if not h.get("style"):
            h["style"] = style
    return horses
