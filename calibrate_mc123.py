"""
mc123_engine.py の5プレースホルダ係数(K_ABILITY, GRIT_SCALE, WIND_A_PACE_SHIFT,
WIND_B_STYLE, WIND_C_GUST)を、Brier score最小化により較正する。

Walk-Forward: 係数探索は2021-2022年のみで実施(最速の学習ウィンドウ、リーク防止のため
評価年のデータは一切使わない)。得られた係数を2023/2024/2025の3年に対してOOS評価する
(k_cls等の「構造的パラメータは全期間1回較正で十分」というPhase1の方針を踏襲し、
fold毎の再探索はしない — 探索コストが時間予算を超えるため)。

探索: 5パラメータをscipy.optimize不使用の座標降下法(coordinate descent)で最適化。
MCシミュレーション自体は微分不可能なブラックボックスのため。
"""
import sqlite3
import time
import math
from collections import defaultdict

import mc123_engine
from mc123_engine import run_mc123, hash64_seed, N_MC_DEFAULT
from mc123_batch import (
    load_horse_hist_all, load_same_day_bias_dict, precompute_horse_features_fast,
)
from build_class_par import build_class_par_table, calibrate_k_cls
from build_pace_baseline import build_baseline_table

DB_PATH = "keiba.db"


def umaban_to_gate(umaban):
    if not umaban or umaban <= 0:
        return 4
    return min((umaban + 1) // 2, 8)


def load_year_races(conn, year_lo, year_hi, horse_hist, class_par, k_cls, bias_map,
                     pace_baseline, wind_map, rank_par=None, margin_par=None, l3f_par=None,
                     verbose=True):
    """year_lo<=年<year_hi の全レースについて、特徴量計算済みhorses+race_infoのリストを返す
    (係数を変えて何度もMCを再実行するための「特徴量固定・係数だけ変える」キャッシュ)。
    rank_par/margin_par/l3f_par(2026-07-20追加)を渡すとrfa_rank_z/rfa_margin_z/l3f_zも
    同時に計算する。"""
    races = conn.execute("""
        SELECT DISTINCT r.date, r.venue, r.race_num, r.race_id, r.surface, r.distance, r.track_cond
        FROM results r
        WHERE r.date >= ? AND r.date < ?
          AND r.surface IN ('芝','ダ')
          AND r.race_name NOT LIKE '%新馬%' AND r.race_name NOT LIKE '%未勝利%'
        ORDER BY r.date, r.venue, r.race_num
    """, (f"{year_lo}-01-01", f"{year_hi}-01-01")).fetchall()

    out = []
    for date, venue, race_num, race_id, srf, dist, tc in races:
        runners = conn.execute(
            "SELECT horse_name, umaban, jockey, finish, horse_num FROM results WHERE race_id=?",
            (race_id,)
        ).fetchall()
        horses = []
        for hn, uma, jk, fin, hnum in runners:
            hn = (hn or "").strip()
            if not hn:
                continue
            u = uma if uma is not None else hnum
            horses.append({"horse_name": hn, "umaban": u, "jockey": (jk or "").strip(),
                            "gate": umaban_to_gate(u), "style": None, "finish": fin})
        if len(horses) < 4:
            continue
        race_info = {"venue": venue, "distance": dist or 1600, "track_cond": tc or "良",
                     "num_horses": len(horses), "date": date, "surface": srf, "race_id": race_id}
        precompute_horse_features_fast(horses, race_info, horse_hist, class_par, k_cls,
                                        bias_map, pace_baseline, rank_par=rank_par,
                                        margin_par=margin_par, l3f_par=l3f_par)
        wind = wind_map.get(race_id)
        seed = hash64_seed(race_id)
        out.append((horses, race_info, wind, seed))
    if verbose:
        print(f"  {year_lo}-{year_hi-1}年: {len(out)}レース 特徴量計算済み")
    return out


def brier_and_logloss(prepared_races, n_mc=N_MC_DEFAULT):
    """現在のmc123_engineモジュール係数(グローバル変数)を使ってMCを実行し、
    p1に対するBrier score・log-lossを計算する(勝ち馬=1, 他=0の2値予測として)。"""
    sq_err_sum, ll_sum, n_obs = 0.0, 0.0, 0
    eps = 1e-6
    for horses, race_info, wind, seed in prepared_races:
        result = run_mc123(horses, race_info, n_mc=n_mc, seed=seed, wind=wind)
        for h, r in zip(horses, result):
            y = 1.0 if (h["finish"] is not None and float(h["finish"]) == 1.0) else 0.0
            p = min(max(r["p1"], eps), 1 - eps)
            sq_err_sum += (p - y) ** 2
            ll_sum += -(y * math.log(p) + (1 - y) * math.log(1 - p))
            n_obs += 1
    return sq_err_sum / n_obs, ll_sum / n_obs, n_obs


def set_coefs(coefs):
    mc123_engine.K_ABILITY = coefs["K_ABILITY"]
    mc123_engine.GRIT_SCALE = coefs["GRIT_SCALE"]
    mc123_engine.WIND_A_PACE_SHIFT = coefs["WIND_A_PACE_SHIFT"]
    mc123_engine.WIND_B_STYLE = coefs["WIND_B_STYLE"]
    mc123_engine.WIND_C_GUST = coefs["WIND_C_GUST"]
    mc123_engine.K_RANK = coefs["K_RANK"]
    mc123_engine.K_MARGIN = coefs["K_MARGIN"]
    mc123_engine.K_L3F = coefs["K_L3F"]


# PLACEHOLDER: 前回較正(2026-07-20 1回目)の結果を既存5係数の起点として使う
# (前回較正済みの値を再度探索の初期値にするのは自然で、プレースホルダに巻き戻す理由がない)。
# 新規3係数(K_RANK/K_MARGIN/K_L3F)はプランナー指定通り1.0を初期値とする。
PLACEHOLDER = {
    "K_ABILITY": 1.0, "GRIT_SCALE": 10.0, "WIND_A_PACE_SHIFT": 0.01,
    "WIND_B_STYLE": 0.05, "WIND_C_GUST": 0.15,
    "K_RANK": 1.0, "K_MARGIN": 1.0, "K_L3F": 1.0,
}

# 座標降下法の探索候補。
# 【2026-07-20 時間予算対応】8係数になり探索コストが増えるため、既存5係数の候補数を
# 5→3に削減(前回較正値を中心に±方向のみ残す)。新規3係数も3候補ずつとする。
# 8パラメータ×3候補×最大2ラウンド=48回評価が上限(実際は早期収束で減る想定)。
CANDIDATES = {
    "K_ABILITY": [0.5, 1.0, 2.0],
    "GRIT_SCALE": [5.0, 10.0, 20.0],
    "WIND_A_PACE_SHIFT": [0.0, 0.01, 0.02],
    "WIND_B_STYLE": [0.0, 0.05, 0.15],
    "WIND_C_GUST": [0.0, 0.15, 0.3],
    "K_RANK": [0.3, 1.0, 2.0],
    "K_MARGIN": [0.3, 1.0, 2.0],
    "K_L3F": [0.3, 1.0, 2.0],
}


def coordinate_descent(prepared_train, n_mc=N_MC_DEFAULT, rounds=2, verbose=True):
    best = dict(PLACEHOLDER)
    set_coefs(best)
    best_brier, best_ll, n = brier_and_logloss(prepared_train, n_mc=n_mc)
    if verbose:
        print(f"  初期(placeholder) Brier={best_brier:.6f} logloss={best_ll:.6f} n={n}")

    n_evals = 1
    for rd in range(rounds):
        improved_this_round = False
        for param in CANDIDATES:
            local_best_val = best[param]
            local_best_brier = best_brier
            for cand in CANDIDATES[param]:
                trial = dict(best)
                trial[param] = cand
                set_coefs(trial)
                brier, ll, _ = brier_and_logloss(prepared_train, n_mc=n_mc)
                n_evals += 1
                if brier < local_best_brier:
                    local_best_brier = brier
                    local_best_val = cand
            if local_best_val != best[param]:
                best[param] = local_best_val
                best_brier = local_best_brier
                improved_this_round = True
                if verbose:
                    print(f"  round{rd+1} {param} -> {local_best_val} (Brier={best_brier:.6f})")
        if not improved_this_round:
            if verbose:
                print(f"  round{rd+1}: 変化なし、収束")
            break

    set_coefs(best)
    final_brier, final_ll, n = brier_and_logloss(prepared_train, n_mc=n_mc)
    if verbose:
        print(f"  最終(較正後) Brier={final_brier:.6f} logloss={final_ll:.6f}  評価回数={n_evals}")
    return best, final_brier, final_ll, n_evals
