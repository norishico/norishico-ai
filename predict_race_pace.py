# -*- coding: utf-8 -*-
"""
predict_race_pace.py — mc_dyn(案C)を使った個別レースの展開予想ツール(2026-08-01追加)。

venue/surface/distance/track_cond(馬場状態)と出走馬の脚質構成を与えると、
n_sim回(既定10,000)のモンテカルロシミュレーションからペース予想(H/M/S確率)と
平均ラップチャートを出力する。

使い方:
    python predict_race_pace.py --venue 東京 --surface 芝 --distance 1600 \
        --track-cond 良 --nige 1 --senko 3 --sashi 4 --oikomi 4

較正済みパラメータ(mc_dyn_engine.pyのモジュール既定値)をそのまま使用する。
2026-08-05時点でPhase2 3ゲート全合格(gate1相関0.906/gate2新潟H率91.7%/
gate3新基準3a+33.6pt・3bバイアス+0.4pt・3c +5.4pt)。単騎逃げイージング(solo_ease)、
ダッシュ窓距離テーパー(dash_cap_for)+芝のd_c1非依存化(dash_window_for、直線コース除外)、
レースレベルのペース意図ノイズ(pace_noise)、レース属性ペースバイアス(pace_bias v2、
クラス×距離帯交互作用+頭数+直線長[262-526mクリップ]を距離帯別応答係数で変換 —
--race-classで指定、検証会場OOSで|bias|14.2→11.3pt改善)を含む。
"""
import argparse
import random
import sqlite3

from mc_dyn_engine import (
    anchor_v_base, simulate_field, build_slope_zones, TRACK_COND_V_FACTOR,
    KAPPA_PRESS_DIRT, KAPPA_PRESS_TURF, RHO_SAVE, A_LAT, K0, PHI_FADE,
    DASH_MIN_FRAC, DASH_RIVAL_SAT, CHUTE_DASH_FRAC, D_SCALE_TURF,
    SOLO_EASE_SCALE_TURF, SOLO_EASE_SCALE_DIRT, EASE_RIVAL_SAT, dash_cap_for,
    PACE_NOISE_SIGMA_TURF, PACE_NOISE_SIGMA_DIRT,
    NIGE_SETTLE_PROB_TURF, NIGE_SETTLE_PROB_DIRT, K_SLOPE, K_SLOPE_DIRT,
    slope_intent_bias, SLOPE_INTENT_COEF_DIRT, SLOPE_INTENT_COEF_TURF,
    dash_window_for, pace_bias_for, pace_cls_group,
)
from calibrate_mc_dyn_phase2 import (
    get_par_time, get_course_geometry, get_slope_defs, compute_q_star,
)
from fetch_laps import calc_derived

DB_PATH = "keiba.db"
STYLES = ["逃げ", "先行", "差し", "追い込み"]


def predict(conn, venue, surface, distance, track_cond, style_counts, n_sim=10000,
            dt=0.5, horse_noise_sd=3.0, seed_base=0, race_class=None):
    """race_class(2026-08-05追加): '新馬'/'未勝利'/'勝上'のクラス群、またはレース名文字列
    (pace_cls_groupで判定)。None=勝上扱い。頭数はstyle_countsの合計から自動算出し、
    直線長はコース幾何から取得して、レース属性ペースバイアスに反映する。"""
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
    # レースレベルのペース意図ノイズ(2026-08-05追加・較正済み)。実測の前後半バランス
    # ばらつき(観測可能な事前情報では説明できないレース固有変動、std0.31-0.46)を再現する。
    pace_noise_sigma = PACE_NOISE_SIGMA_DIRT if surface == "ダ" else PACE_NOISE_SIGMA_TURF
    # 複数逃げの先導権決着(2026-08-07追加・較正済み)。「逃げ分類が複数いても実戦では
    # 一方が譲る」戦術的裁量をレースレベル抽選で表現(mc_dyn_engine.NIGE_SETTLE_PROB参照)。
    nige_settle_prob = NIGE_SETTLE_PROB_DIRT if surface == "ダ" else NIGE_SETTLE_PROB_TURF
    apply_chute_boost = bool(geometry.get("is_chute", False)) and surface == "ダ"
    track_cond_factor = TRACK_COND_V_FACTOR.get(surface, {}).get(track_cond, 1.0)

    v_base, _ = anchor_v_base(distance, par_time, k0=K0, phi_fade=PHI_FADE,
                               slope_zones=slope_zones,
                               # アンカーもシミュ本体と同一のk_slopeを使う(不一致だと坂分だけ総時間がずれる)
                               k_slope=K_SLOPE_DIRT if surface == "ダ" else K_SLOPE)

    q_star, _ = compute_q_star(conn)

    styles = []
    for style, c in style_counts.items():
        styles.extend([style] * c)
    if len(styles) < 2:
        raise ValueError("出走頭数は2頭以上必要")

    # レース属性ペースバイアス(2026-08-05追加・較正済み)。クラス(新馬/未勝利/勝上)・
    # 頭数・直線長から先頭馬の巡航速度シフトを計算(会場名は不使用の一般則)。
    # コーナー無しコース(新潟芝1000等)は対象外(has_corners=False→0)。
    cls_group = pace_cls_group(race_class) if race_class else "勝上"
    has_corners = bool(geometry["corner_zones"])
    pace_bias = pace_bias_for(surface, cls_group, len(styles), geometry.get("straight_home"),
                              distance=distance, has_corners=has_corners) \
        + slope_intent_bias(slope_zones, distance,
                            SLOPE_INTENT_COEF_DIRT if surface == "ダ" else SLOPE_INTENT_COEF_TURF)

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
            d_scale=d_scale, slope_zones=slope_zones,
            # k_slope表面別選択(2026-08-07): 芝=K_SLOPE(0)、ダ=K_SLOPE_DIRT(道中勾配較正済み)
            k_slope=K_SLOPE_DIRT if surface == "ダ" else K_SLOPE,
            solo_ease_scale=solo_ease_scale, ease_rival_sat=EASE_RIVAL_SAT,
            nige_settle_prob=nige_settle_prob,
            pace_noise_sigma=pace_noise_sigma, pace_bias=pace_bias,
            dash_cap_m=dash_cap_for(surface, distance),
            dash_window_m=dash_window_for(surface, distance) if has_corners else None,
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
    ap.add_argument("--race-class", default=None,
                    help="クラス群(新馬/未勝利/勝上)またはレース名。省略時は勝上扱い")
    args = ap.parse_args()

    style_counts = {"逃げ": args.nige, "先行": args.senko, "差し": args.sashi, "追い込み": args.oikomi}

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    result = predict(conn, args.venue, args.surface, args.distance, args.track_cond,
                      style_counts, n_sim=args.n_sim, race_class=args.race_class)
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
