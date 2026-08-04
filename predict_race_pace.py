# -*- coding: utf-8 -*-
"""
predict_race_pace.py — mc_dyn(案C)を使った個別レースの展開予想ツール(2026-08-01追加)。

venue/surface/distance/track_cond(馬場状態)と出走馬の脚質構成を与えると、
n_sim回(既定10,000)のモンテカルロシミュレーションからペース予想(H/M/S確率)と
平均ラップチャートを出力する。

使い方:
    python predict_race_pace.py --venue 東京 --surface 芝 --distance 1600 \
        --track-cond 良 --nige 1 --senko 3 --sashi 4 --oikomi 4

較正済みパラメータ(mc_dyn_engine.pyのモジュール既定値、2026-08-01時点でPhase2
3ゲート全合格: 相関0.829/新潟H率91.7%/単騎S5.2%>複数S3.6%)をそのまま使用する。
"""
import argparse
import random
import sqlite3

from mc_dyn_engine import (
    anchor_v_base, simulate_field, build_slope_zones, TRACK_COND_V_FACTOR,
    KAPPA_PRESS_DIRT, KAPPA_PRESS_TURF, RHO_SAVE, A_LAT, K0, PHI_FADE,
    DASH_MIN_FRAC, DASH_RIVAL_SAT, CHUTE_DASH_FRAC, D_SCALE_TURF,
    SOLO_EASE_SCALE_TURF, SOLO_EASE_SCALE_DIRT, EASE_RIVAL_SAT,
)
from calibrate_mc_dyn_phase2 import (
    get_par_time, get_course_geometry, get_slope_defs, compute_q_star,
)
from fetch_laps import calc_derived

DB_PATH = "keiba.db"
STYLES = ["逃げ", "先行", "差し", "追い込み"]


def predict(conn, venue, surface, distance, track_cond, style_counts, n_sim=10000,
            dt=0.5, horse_noise_sd=3.0, seed_base=0):
    par_time = get_par_time(conn, venue, surface, distance)
    if par_time is None:
        raise ValueError(f"par_time取得不可: {venue}{surface}{distance}m")
    geometry = get_course_geometry(conn, venue, surface, distance)
    if geometry is None:
        raise ValueError(f"コース幾何データなし: {venue}{surface}{distance}m")

    slope_defs = get_slope_defs(conn, venue, surface)
    slope_zones = build_slope_zones(distance, slope_defs)

    kappa_press = KAPPA_PRESS_DIRT if surface == "ダ" else KAPPA_PRESS_TURF
    d_scale = 1.0 if surface == "ダ" else D_SCALE_TURF
    # 構成依存イージング(単騎逃げの余裕、2026-08-04追加・較正済み)。逃げ馬頭数が
    # 少ないほどスローペースになりやすい実測傾向(芝: 単騎S率47.1% vs 複数20.0%)を反映。
    solo_ease_scale = SOLO_EASE_SCALE_DIRT if surface == "ダ" else SOLO_EASE_SCALE_TURF
    apply_chute_boost = bool(geometry.get("is_chute", False)) and surface == "ダ"
    track_cond_factor = TRACK_COND_V_FACTOR.get(surface, {}).get(track_cond, 1.0)

    v_base, _ = anchor_v_base(distance, par_time, k0=K0, phi_fade=PHI_FADE,
                               slope_zones=slope_zones, k_slope=0.0)

    q_star, _ = compute_q_star(conn)

    styles = []
    for style, c in style_counts.items():
        styles.extend([style] * c)
    if len(styles) < 2:
        raise ValueError("出走頭数は2頭以上必要")

    rng = random.Random(seed_base)
    counts = {"H": 0, "M": 0, "S": 0, "none": 0}
    lap_sums, lap_counts = [], []

    for i in range(n_sim):
        horses = [{"style": st,
                   "spd": 80.0 + rng.gauss(0, horse_noise_sd),
                   "spr": 80.0 + rng.gauss(0, horse_noise_sd),
                   "sta": 75.0 + rng.gauss(0, horse_noise_sd)} for st in styles]
        result = simulate_field(
            distance, v_base, geometry["d_c1"], geometry["corner_zones"], horses, q_star,
            k0=K0, phi_fade=PHI_FADE,
            kappa_press=kappa_press, rho_save=RHO_SAVE, a_lat=A_LAT,
            dash_min_frac=DASH_MIN_FRAC, dash_rival_sat=DASH_RIVAL_SAT,
            is_chute_start=apply_chute_boost, chute_dash_frac=CHUTE_DASH_FRAC,
            d_scale=d_scale, slope_zones=slope_zones, k_slope=0.0,
            solo_ease_scale=solo_ease_scale, ease_rival_sat=EASE_RIVAL_SAT,
            track_cond_factor=track_cond_factor,
            dt=dt, seed=seed_base * 1_000_003 + i,
        )
        derived = calc_derived(result["leader_laps"], distance)
        pt = derived.get("pace_type")
        if pt in ("H", "M", "S"):
            counts[pt] += 1
        else:
            counts["none"] += 1
        laps = result["leader_laps"]
        if not lap_sums:
            lap_sums = list(laps)
            lap_counts = [1] * len(laps)
        else:
            for j, lap in enumerate(laps):
                if j < len(lap_sums):
                    lap_sums[j] += lap
                    lap_counts[j] += 1

    total = counts["H"] + counts["M"] + counts["S"]
    avg_laps = [s / c for s, c in zip(lap_sums, lap_counts)] if total else []
    return {
        "venue": venue, "surface": surface, "distance": distance, "track_cond": track_cond,
        "track_cond_factor": track_cond_factor,
        "n_sim": n_sim, "n_valid": total, "n_none": counts["none"],
        "h_rate": counts["H"] / total if total else None,
        "m_rate": counts["M"] / total if total else None,
        "s_rate": counts["S"] / total if total else None,
        "avg_leader_laps": avg_laps,
        "avg_total_time": sum(avg_laps) if avg_laps else None,
        "par_time": par_time,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", required=True)
    ap.add_argument("--surface", required=True, choices=["芝", "ダ"])
    ap.add_argument("--distance", type=int, required=True)
    ap.add_argument("--track-cond", default="良", choices=["良", "稍", "重", "不", "不良"])
    ap.add_argument("--nige", type=int, default=1)
    ap.add_argument("--senko", type=int, default=3)
    ap.add_argument("--sashi", type=int, default=4)
    ap.add_argument("--oikomi", type=int, default=4)
    ap.add_argument("--n-sim", type=int, default=10000)
    args = ap.parse_args()

    style_counts = {"逃げ": args.nige, "先行": args.senko, "差し": args.sashi, "追い込み": args.oikomi}

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    result = predict(conn, args.venue, args.surface, args.distance, args.track_cond,
                      style_counts, n_sim=args.n_sim)
    conn.close()

    print(f"{result['venue']}{result['surface']}{result['distance']}m  "
          f"馬場={result['track_cond']}(速度倍率{result['track_cond_factor']:.4f})  "
          f"n_sim={result['n_sim']}(有効{result['n_valid']}件)")
    print(f"ペース予想: H={result['h_rate']*100:.1f}%  M={result['m_rate']*100:.1f}%  "
          f"S={result['s_rate']*100:.1f}%")
    print(f"平均想定タイム: {result['avg_total_time']:.2f}秒 "
          f"(par_time参考値={result['par_time']:.2f}秒)")
    print("平均ラップ(先頭通過、秒/区間):")
    print("  " + " / ".join(f"{l:.2f}" for l in result["avg_leader_laps"]))


if __name__ == "__main__":
    main()
