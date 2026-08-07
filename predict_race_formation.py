# -*- coding: utf-8 -*-
"""
predict_race_formation.py — mc_dynによる4フェーズ隊列(順位)予測 (2026-08-03新規)

のりお要望「レースを序盤・中盤・終盤・直線に分けて各馬の隊列を予測できないか」の実装。
mc_dyn_engine.simulate_field(record_snapshots=True) の順位スナップショットを使い、
以下の4フェーズ境界の予測隊列を出力する(境界はモデル内の既存の物理的節目を流用):
  序盤終わり = d_c1(発走〜第1コーナー入口)
  中盤終わり = kick_trigger(仕掛け開始点)
  終盤       = 最終コーナー(corner_zones[-1])の入口/出口
  直線→ゴール = distance

脚質入力は classify_style_c2(generate_race_sim.py、2021-2023学習・凍結、レース前情報のみ)
を使用 — 当該レースの実測pos4等は一切使わない(循環参照なし)。

実データとの答え合わせ(--validate):
  モデルの最終コーナー入口通過順位 ⟷ results.pos3 (3角=最終ターン入口)
  モデルの最終コーナー出口通過順位 ⟷ results.pos4 (4角=最終ターン出口)
  モデルのゴール順位             ⟷ results.finish
  ※pos1/pos2は約57%が0埋めセンチネルのため検証対象外(既知のデータ品質問題)。
  ※「序盤」(d_c1)は実データに対応する信頼できる実測値がなく直接検証不能。
  ※「直線」はゴール以外の中間実測がないためゴールでのみ検証。
  ベースラインとして「classify_style_c2の連続スコア順位のみ(シミュレーションなし)」
  との相関も併記する(シミュの追加価値を分離するため)。

使い方:
  py -3 predict_race_formation.py --race-id 2026-07-26_中京_11
  py -3 predict_race_formation.py --validate --n-races 300 --n-sim 80
"""
import sys
import io
import json
import time
import random
import argparse
import sqlite3
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from mc_dyn_engine import (
    simulate_field, build_slope_zones, anchor_v_base,
    KAPPA_PRESS_DIRT, KAPPA_PRESS_TURF, RHO_SAVE, A_LAT, K0, PHI_FADE,
    DASH_MIN_FRAC, DASH_RIVAL_SAT, CHUTE_DASH_FRAC, D_SCALE_TURF,
    TRACK_COND_V_FACTOR,
    SOLO_EASE_SCALE_TURF, SOLO_EASE_SCALE_DIRT, EASE_RIVAL_SAT, dash_cap_for,
    PACE_NOISE_SIGMA_TURF, PACE_NOISE_SIGMA_DIRT,
    NIGE_SETTLE_PROB_TURF, NIGE_SETTLE_PROB_DIRT, K_SLOPE, K_SLOPE_DIRT,
    slope_intent_bias, SLOPE_INTENT_COEF_DIRT, SLOPE_INTENT_COEF_TURF,
    dash_window_for, pace_bias_for, pace_cls_group,
)
from calibrate_mc_dyn_phase2 import (
    get_par_time, get_course_geometry, get_slope_defs, compute_q_star,
)
from mc_dyn_engine import build_slope_zones as _bsz  # noqa (明示)
from fetch_laps import calc_derived

# 【2026-08-04追加】隊列表示のグループ分け(netkeiba「レース展開予想」の
# 先頭/先団/中団/後方という4区分表記に合わせる)。相関ρ≈0.45-0.5程度の予測力しかない
# ため、1位2位…と厳密な順位を断定するより、групп分けの方が実態に即した見せ方になる。
TIER_LABELS = ["先頭", "先団", "中団", "後方"]
TIER_CUTOFFS = [0.15, 0.40, 0.75]  # rank_ratio <= cutoffで所属先を決める(超えたら次の区分)


def rank_to_tier(rank, n):
    """0始まりrankを先頭/先団/中団/後方の4区分に変換する。"""
    ratio = (rank + 1) / n
    for cutoff, label in zip(TIER_CUTOFFS, TIER_LABELS):
        if ratio <= cutoff:
            return label
    return TIER_LABELS[-1]

DB_PATH = "keiba.db"
_COURSE_CACHE: dict = {}
_QSTAR_CACHE: dict = {}


def get_course_bundle(conn, venue, surface, distance):
    key = (venue, surface, distance)
    if key in _COURSE_CACHE:
        return _COURSE_CACHE[key]
    par_time = get_par_time(conn, venue, surface, distance)
    geometry = get_course_geometry(conn, venue, surface, distance) if par_time else None
    if par_time is None or geometry is None:
        _COURSE_CACHE[key] = None
        return None
    slope_defs = get_slope_defs(conn, venue, surface)
    slope_zones = build_slope_zones(distance, slope_defs)
    v_base, _ = anchor_v_base(distance, par_time, k0=K0, phi_fade=PHI_FADE,
                              slope_zones=slope_zones,
                              # k_slope表面別選択(2026-08-07、predict_race_pace.pyと同一配線)
                              k_slope=K_SLOPE_DIRT if surface == "ダ" else K_SLOPE)
    bundle = {"v_base": v_base, "geometry": geometry, "slope_zones": slope_zones}
    _COURSE_CACHE[key] = bundle
    return bundle


def get_q_star(conn):
    if "q" not in _QSTAR_CACHE:
        _QSTAR_CACHE["q"], _ = compute_q_star(conn)
    return _QSTAR_CACHE["q"]


def fetch_race(conn, race_id):
    row = conn.execute("""
        SELECT date, venue, surface, distance, num_horses, track_cond, race_name
        FROM results WHERE race_id = ? LIMIT 1""", (race_id,)).fetchone()
    if row is None:
        return None, None
    race = {"date": row[0], "venue": row[1], "surface": row[2], "distance": row[3],
            "num_horses": row[4], "track_cond": row[5], "race_name": row[6]}
    horses = [{"horse_name": r[0], "jockey": r[1] or "", "sire": r[2],
               "umaban": r[3], "weight_kg": r[4], "horse_weight": r[5],
               "pos3": r[6], "pos4": r[7], "finish": r[8],
               "pos1": r[9], "pos2": r[10]}
              for r in conn.execute("""
        SELECT TRIM(horse_name), jockey, TRIM(sire), umaban, weight_kg,
               horse_weight, pos3, pos4, finish, pos1, pos2
        FROM results WHERE race_id = ? AND finish < 90""", (race_id,))]
    return race, horses


def predict_formation(conn, race, horses, n_sim=100, seed=0):
    """4フェーズ隊列予測。戻り値: (フェーズ名→[馬indexの予測順], 馬index→c2結果, 詳細)"""
    from generate_race_sim import classify_style_c2
    # 【2026-08-06追加】fetch_race()は"finish < 90"で絞るため、まだ確定していない
    # レース(finish全てNULL)ではhorsesが0件になり、simulate_field()のn<2早期
    # リターン(snapshotsキー無し)でKeyErrorになっていたバグを修正。
    if len(horses) < 2:
        return {"excluded": True, "reason": "着順が未確定(finish未反映)、または出走2頭未満のため予測できません"}
    # 【2026-08-05追加】新馬戦は除外。新馬は出走馬全員が過去走ゼロのため
    # classify_style_c2が騎手×種牡馬フォールバックのみに頼ることになり、
    # 実測検証(2026-01〜07月、n=89 vs 未勝利n=150/1勝クラスn=150)で
    # 相関ρ=0.289と、未勝利0.509・1勝クラス0.516に比べ明確に低い(のりお指摘、
    # 実データで確認済み)。未勝利は過去走が平均3.4走と少ないが精度は通常水準
    # (0.509)のため対象外にしない。
    if pace_cls_group(race.get("race_name")) == "新馬":
        return {"excluded": True, "reason": "新馬戦は過去走情報が無く、隊列予測の精度が実測で確認できないため対象外です"}
    bundle = get_course_bundle(conn, race["venue"], race["surface"], race["distance"])
    if bundle is None:
        return None
    c2 = classify_style_c2(conn, race, horses)
    styles = [r["style"] for r in c2]
    q_star = get_q_star(conn)
    geometry, slope_zones = bundle["geometry"], bundle["slope_zones"]
    v_base = bundle["v_base"]
    surface = race["surface"]
    kappa_press = KAPPA_PRESS_DIRT if surface == "ダ" else KAPPA_PRESS_TURF
    d_scale = 1.0 if surface == "ダ" else D_SCALE_TURF
    apply_chute = bool(geometry.get("is_chute", False)) and surface == "ダ"
    # 構成依存イージング(単騎逃げの余裕、2026-08-04追加・較正済み、predict_race_pace.pyと同一配線)
    solo_ease_scale = SOLO_EASE_SCALE_DIRT if surface == "ダ" else SOLO_EASE_SCALE_TURF
    # レースレベルのペース意図ノイズ(2026-08-05追加・較正済み、predict_race_pace.pyと同一配線)
    pace_noise_sigma = PACE_NOISE_SIGMA_DIRT if surface == "ダ" else PACE_NOISE_SIGMA_TURF
    # 複数逃げの先導権決着(2026-08-07追加・較正済み、predict_race_pace.pyと同一配線)
    nige_settle_prob = NIGE_SETTLE_PROB_DIRT if surface == "ダ" else NIGE_SETTLE_PROB_TURF
    # レース属性ペースバイアス(2026-08-05追加): クラス(race_nameから判定)・頭数・直線長。
    # コーナー無しコース(直線競走)は対象外。
    has_corners = bool(geometry["corner_zones"])
    pace_bias = pace_bias_for(surface, pace_cls_group(race.get("race_name")),
                              len(horses), geometry.get("straight_home"),
                              distance=race["distance"], has_corners=has_corners) \
        + slope_intent_bias(slope_zones, race["distance"],
                            SLOPE_INTENT_COEF_DIRT if surface == "ダ" else SLOPE_INTENT_COEF_TURF)
    # 【2026-08-04修正】track_cond_factorが未配線でmc_dynのTRACK_COND_V_FACTORが
    # 一切反映されていなかったバグを修正(画面には「馬場=良」等と表示するのに計算には
    # 使っていなかった)。未知の表記/欠損時は1.0(無補正)にフォールバックする。
    track_cond_factor = TRACK_COND_V_FACTOR.get(surface, {}).get(race.get("track_cond"), 1.0)

    n = len(horses)
    rng = random.Random(seed)
    rank_sums = defaultdict(lambda: [0.0] * n)
    n_zones = len(geometry["corner_zones"])
    pace_counts = {"H": 0, "M": 0, "S": 0, "none": 0}
    lap_sums, lap_counts = [], []
    for k in range(n_sim):
        sim_horses = [{"style": st, "spd": 80.0 + rng.gauss(0, 3.0),
                       "spr": 80.0 + rng.gauss(0, 3.0),
                       "sta": 75.0 + rng.gauss(0, 3.0)} for st in styles]
        res = simulate_field(
            race["distance"], v_base, geometry["d_c1"], geometry["corner_zones"],
            sim_horses, q_star, k0=K0, phi_fade=PHI_FADE, kappa_press=kappa_press,
            rho_save=RHO_SAVE, a_lat=A_LAT, dash_min_frac=DASH_MIN_FRAC,
            dash_rival_sat=DASH_RIVAL_SAT, is_chute_start=apply_chute,
            chute_dash_frac=CHUTE_DASH_FRAC, d_scale=d_scale,
            solo_ease_scale=solo_ease_scale, ease_rival_sat=EASE_RIVAL_SAT,
            nige_settle_prob=nige_settle_prob,
            pace_noise_sigma=pace_noise_sigma, pace_bias=pace_bias,
            dash_cap_m=dash_cap_for(surface, race["distance"]),
            dash_window_m=(dash_window_for(surface, race["distance"])
                           if has_corners else None),
            slope_zones=slope_zones,
            k_slope=K_SLOPE_DIRT if surface == "ダ" else K_SLOPE, dt=0.5,
            track_cond_factor=track_cond_factor,
            seed=seed * 1_000_003 + k, record_snapshots=True,
        )
        ranks = res["snapshots"]["ranks"]
        for name, rk in ranks.items():  # 全チェックポイントを集計(zone0はpos1/2検証用)
            for i in range(n):
                rank_sums[name][i] += rk[i]
        derived = calc_derived(res["leader_laps"], race["distance"])
        pt = derived.get("pace_type")
        pace_counts[pt if pt in ("H", "M", "S") else "none"] += 1
        laps = res["leader_laps"]
        if not lap_sums:
            lap_sums = list(laps)
            lap_counts = [1] * len(laps)
        else:
            for j, lap in enumerate(laps):
                if j < len(lap_sums):
                    lap_sums[j] += lap
                    lap_counts[j] += 1

    mean_ranks = {name: [v / n_sim for v in vals] for name, vals in rank_sums.items()}
    phase_map = {
        "序盤(1角入口)": "phase_c1", "中盤(仕掛け点)": "phase_kick",
        "終盤(最終C入口≒3角)": f"zone{n_zones-1}_in",
        "直線入口(最終C出口≒4角)": f"zone{n_zones-1}_out", "ゴール": "goal",
    }
    formation = {}
    for label, key in phase_map.items():
        if key in mean_ranks:
            formation[label] = sorted(range(n), key=lambda i: mean_ranks[key][i])

    n_valid = pace_counts["H"] + pace_counts["M"] + pace_counts["S"]
    avg_laps = [s / c for s, c in zip(lap_sums, lap_counts)] if n_valid else []
    pace = {
        "h_rate": pace_counts["H"] / n_valid if n_valid else None,
        "m_rate": pace_counts["M"] / n_valid if n_valid else None,
        "s_rate": pace_counts["S"] / n_valid if n_valid else None,
        "avg_total_time": sum(avg_laps) if avg_laps else None,
        "par_time": get_par_time(conn, race["venue"], race["surface"], race["distance"]),
    }
    nige_count = sum(1 for st in styles if st == "逃げ")
    return {"formation": formation, "mean_ranks": mean_ranks, "c2": c2,
            "phase_map": phase_map, "pace": pace, "nige_count": nige_count}


def describe_pace(pace, nige_count, n_horses):
    """ペース統計+逃げ馬頭数+基準タイム差から、日本語の解説文を1段落で生成する。"""
    h, m, s = pace["h_rate"], pace["m_rate"], pace["s_rate"]
    if h is None:
        return "ペース判定に必要なシミュレーション結果が不足しています。"
    dominant = max(("H", h), ("M", m), ("S", s), key=lambda x: x[1])[0]
    if dominant == "H":
        lean = f"ハイペースが濃厚(H={h*100:.0f}%)で、前が苦しくなり差し・追い込み勢にチャンスが増えそうな展開です。"
    elif dominant == "S":
        lean = f"スローペースが濃厚(S={s*100:.0f}%)で、前残りしやすく、逃げ・先行勢が有利な展開が見込まれます。"
    else:
        second = "H" if h >= s else "S"
        if second == "H" and h >= 0.20:
            lean = (f"ミドルペースが中心(M={m*100:.0f}%)ですが、ハイペースに振れる可能性"
                    f"(H={h*100:.0f}%)もあり、前同士の主導権争い次第では差し・追い込みにもチャンスが残ります。")
        elif second == "S" and s >= 0.20:
            lean = (f"ミドルペースが中心(M={m*100:.0f}%)ですが、スローペースに振れる可能性"
                    f"(S={s*100:.0f}%)もあり、前が残りやすい展開になることもありそうです。")
        else:
            lean = f"ミドルペースが中心(M={m*100:.0f}%)で、極端に速い/遅い展開にはなりにくそうです。"

    if nige_count == 0:
        nige_txt = "確固たる逃げ馬が不在のため、誰が先手を取るかが展開の鍵になります。"
    elif nige_count == 1:
        nige_txt = "逃げ馬は1頭で、単騎で楽な逃げを打てる可能性があります。"
    else:
        nige_txt = f"逃げ馬が{nige_count}頭おり、主導権争いが激しくなればペースが上がりやすい構成です。"

    time_txt = ""
    if pace["avg_total_time"] and pace["par_time"]:
        diff = pace["par_time"] - pace["avg_total_time"]
        if abs(diff) >= 0.5:
            faster_slower = "速い" if diff > 0 else "遅い"
            time_txt = f" 想定タイムは基準より約{abs(diff):.1f}秒{faster_slower}決着になりそうです。"

    return f"{lean} {nige_txt}{time_txt}"


def cmd_race(args):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    race, horses = fetch_race(conn, args.race_id)
    if race is None:
        print(f"レースが見つかりません: {args.race_id}")
        return
    out = predict_formation(conn, race, horses, n_sim=args.n_sim, seed=1)
    if out is None:
        print("コース情報(par_time/geometry)が無いため予測できません")
        return
    if out.get("excluded"):
        print(f"{args.race_id} {race['race_name']} {race['surface']}{race['distance']}m "
              f"{len(horses)}頭 — 予測対象外: {out['reason']}")
        return
    n = len(horses)
    pace = out["pace"]
    print(f"{args.race_id} {race['race_name']} {race['surface']}{race['distance']}m "
          f"{n}頭 馬場={race['track_cond']}")
    if pace["h_rate"] is not None:
        print(f"予想ペース: H={pace['h_rate']*100:.0f}%  M={pace['m_rate']*100:.0f}%  "
              f"S={pace['s_rate']*100:.0f}%")
        print(describe_pace(pace, out["nige_count"], n))
    print()
    print("隊列予想(馬番+馬名、先頭/先団/中団/後方):")
    # netkeiba「レース展開予想」の3段階(スタート後/3コーナー/4コーナー)に合わせて表示
    main_stages = [("スタート後", "序盤(1角入口)"),
                   ("3コーナー", "終盤(最終C入口≒3角)"),
                   ("4コーナー", "直線入口(最終C出口≒4角)")]
    for stage_label, phase_key in main_stages:
        order = out["formation"].get(phase_key)
        if order is None:
            continue
        tiers = defaultdict(list)
        for rank, i in enumerate(order):
            tier = rank_to_tier(rank, n)
            # 【2026-08-04】umabanは直近日でも大半が欠損(JV-Link経由データの既知の制約、
            # 2026-08-01のみ81%取得できているが他日はほぼ0%)。欠損時は馬名のみ表示する。
            umaban = horses[i].get("umaban")
            label = f"{umaban}{horses[i]['horse_name']}" if umaban else horses[i]['horse_name']
            tiers[tier].append(label)
        parts = [f"{t}[{','.join(tiers[t])}]" for t in TIER_LABELS if tiers[t]]
        print(f"  {stage_label:<8}: " + " ".join(parts))
    print("  ※隊列予想は相関ρ≈0.45程度の中程度の予測力です。厳密な順位ではなく大まかな傾向として参照してください。")
    conn.close()


def cmd_validate(args):
    from scipy.stats import spearmanr
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    races = conn.execute("""
        SELECT DISTINCT race_id FROM results
        WHERE date >= ? AND date < ? AND surface IN ('芝','ダ')
          AND num_horses >= 6 AND pos4 IS NOT NULL AND distance >= ?
    """, (args.start, args.end, args.min_dist)).fetchall()
    random.seed(20260803)
    random.shuffle(races)

    stats = defaultdict(list)   # metric -> [spearman per race]
    n_done = n_skip = 0
    for (race_id,) in races:
        if n_done >= args.n_races:
            break
        race, horses = fetch_race(conn, race_id)
        if race is None or len(horses) < 6:
            n_skip += 1
            continue
        out = predict_formation(conn, race, horses, n_sim=args.n_sim, seed=n_done + 1)
        if out is None or out.get("excluded"):
            n_skip += 1
            continue
        mr = out["mean_ranks"]
        n_zones_key_in = [k for k in mr if k.endswith("_in") and k.startswith("zone")]
        key_in = sorted(n_zones_key_in)[-1] if n_zones_key_in else None
        key_out = key_in.replace("_in", "_out") if key_in else None
        c2_score = [r["score"] for r in out["c2"]]
        checks = [
            ("最終C入口 vs pos3", key_in, "pos3"),
            ("最終C出口 vs pos4", key_out, "pos4"),
            ("ゴール vs finish", "goal", "finish"),
        ]
        # ゾーン2件のレース(=最初のターンが1-2角)のみpos1/pos2でも答え合わせ
        # (pos1/pos2は1700m以上でのみ信頼可 — 距離別欠損率調査より。--min-dist併用)
        if "zone1_in" in mr and "zone2_in" not in mr:
            checks += [("1周目C入口 vs pos1", "zone0_in", "pos1"),
                       ("1周目C出口 vs pos2", "zone0_out", "pos2")]
        for label, key, col in checks:
            if key is None or key not in mr:
                continue
            pairs = [(mr[key][i], h[col]) for i, h in enumerate(horses)
                     if h[col] and h[col] > 0]
            if len(pairs) >= 6:
                rho = spearmanr([p[0] for p in pairs], [p[1] for p in pairs]).statistic
                if rho == rho:
                    stats[label].append(rho)
        # ベースライン: c2スコア順位のみ(シミュなし)
        for label, col in [("c2スコアのみ vs pos3", "pos3"),
                           ("c2スコアのみ vs pos4", "pos4"),
                           ("c2スコアのみ vs finish", "finish")]:
            pairs = [(c2_score[i], h[col]) for i, h in enumerate(horses)
                     if h[col] and h[col] > 0]
            if len(pairs) >= 6:
                rho = spearmanr([p[0] for p in pairs], [p[1] for p in pairs]).statistic
                if rho == rho:
                    stats[label].append(rho)
        n_done += 1
        if n_done % 50 == 0:
            print(f"  {n_done}レース処理済み ({time.time()-t0:.0f}秒)")

    print(f"\n=== 実データ検証 {args.start}..{args.end} "
          f"(n={n_done}レース, スキップ{n_skip}, n_sim={args.n_sim}) ===")
    import statistics
    for label in ["1周目C入口 vs pos1", "1周目C出口 vs pos2",
                  "最終C入口 vs pos3", "c2スコアのみ vs pos3",
                  "最終C出口 vs pos4", "c2スコアのみ vs pos4",
                  "ゴール vs finish", "c2スコアのみ vs finish"]:
        v = stats.get(label, [])
        if v:
            se = statistics.stdev(v) / (len(v) ** 0.5)
            print(f"  {label:<22} 平均ρ={statistics.mean(v):+.4f} "
                  f"(SE {se:.4f}) 中央値={statistics.median(v):+.4f} n={len(v)}")
    print(f"所要時間: {time.time()-t0:.0f}秒")
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--race-id")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--n-races", type=int, default=300)
    ap.add_argument("--n-sim", type=int, default=80)
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-08-01")
    ap.add_argument("--min-dist", type=int, default=0,
                    help="pos1/pos2検証時は1700を指定(それ未満は欠損100%%)")
    args = ap.parse_args()
    if args.race_id:
        args.n_sim = args.n_sim or 100
        cmd_race(args)
    elif args.validate:
        cmd_validate(args)
    else:
        print("使い方: --race-id RACE_ID または --validate")


if __name__ == "__main__":
    main()
