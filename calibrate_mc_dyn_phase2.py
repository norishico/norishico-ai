# -*- coding: utf-8 -*-
"""
calibrate_mc_dyn_phase2.py — mc_dyn Phase2(戦術コントローラ+複数馬シミュレーション)の
検証・軽量較正ハーネス。DBはread-onlyで参照するのみ(keiba.dbへの書き込みは一切しない)。

mc_dyn_engine.py(pure computation)の simulate_field()/build_corner_zones() を、
実際のkeiba.dbデータ(course_layout/course_start_layout/results/race_laps)と組み合わせて
実行する。fetch_laps.pyのcalc_derived()をそのまま再利用してpace_type判定を行う
(ロジックの再実装はしない、という指示を厳守)。

ゲート:
  1. 上位60セル(venue×surface×distance、n>=100)でシミュH率と実測H率の相関 >= 0.7
  2. 新潟ダ1200のシミュH率 >= 90%
  3. 単騎逃げレース(逃げ馬1頭)のシミュS率 > 複数逃げレース(逃げ馬2頭以上)のシミュS率

使い方:
  python calibrate_mc_dyn_phase2.py --quick     # 動作確認(小規模: 8セル x 15シム)
  python calibrate_mc_dyn_phase2.py             # フル実行(60セル x 60シム、初期パラメータ)
  python calibrate_mc_dyn_phase2.py --calibrate # 軽量座標降下法でkappa_press/rho_save/a_latを調整後、フル検証
"""
import sys, os, json, math, random, argparse, statistics, time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mc_dyn_engine import (
    anchor_v_base, segment_lengths, simulate_field, build_corner_zones,
    classify_style_simple, KAPPA_PRESS, K_GAP, RHO_SAVE, A_LAT,
    DASH_MIN_FRAC, DASH_RIVAL_SAT, CHUTE_DASH_FRAC,
    KAPPA_PRESS_DIRT, KAPPA_PRESS_TURF, D_SCALE_TURF,
    build_slope_zones, K_SLOPE,
    SOLO_EASE_SCALE, EASE_RIVAL_SAT, SOLO_EASE_SCALE_TURF, SOLO_EASE_SCALE_DIRT,
    NIGE_SETTLE_PROB_TURF, NIGE_SETTLE_PROB_DIRT,
    DASH_CAP_M, dash_cap_for, dash_window_for,
    PACE_NOISE_SIGMA_TURF, PACE_NOISE_SIGMA_DIRT,
    pace_bias_for, pace_cls_group,
)
from fetch_laps import calc_derived
from calibrate_mc_dyn_phase1 import get_par_time

DB_PATH = "keiba.db"
PHASE1_PARAMS_PATH = "mc_dyn_phase1_params.json"
PHASE2_PARAMS_PATH = "mc_dyn_phase2_params.json"
PHASE2_VERDICT_PATH = "mc_dyn_phase2_verdict.json"

NIIGATA_DA1200 = ("新潟", "ダ", 1200)


# ============================================================================
# DBアクセス関数(このファイルの責務。mc_dyn_engine.pyはDBに触れない)
# ============================================================================

def load_phase1_params():
    with open(PHASE1_PARAMS_PATH, encoding="utf-8") as f:
        return json.load(f)["params"]


def compute_q_star(conn):
    """脚質ごとのpos1/num_horses実測分位点(中央値)。classify_style_simple(単走の実測
    pos4/num_horsesのみで分類、過去走履歴は使わない簡易版)でラベル付けする。"""
    # 【注意】pos1(=0)は「未計測」のセンチネル値であり実際の1位を意味しない。
    # 短距離戦(4角までしか通過順位を計測しない番組が多い)ではpos1/pos2列が0埋めされる
    # ケースが実測で57%にのぼることを確認済み(pos4=0はわずか1845件で信頼できる)。
    # そのためpos1>0(実際に計測されたレースのみ)でフィルタする。
    rows = conn.execute("""
        SELECT pos4, pos1, num_horses FROM results
        WHERE pos1 > 0 AND pos4 IS NOT NULL AND num_horses > 1 AND finish < 90
    """).fetchall()
    groups = defaultdict(list)
    for pos4, pos1, n in rows:
        style = classify_style_simple(pos4, n)
        groups[style].append(pos1 / n)
    q_star = {}
    n_samples = {}
    for style, vals in groups.items():
        q_star[style] = statistics.median(vals)
        n_samples[style] = len(vals)
    return q_star, n_samples


def get_target_cells(conn, min_n=100, top_k=60):
    rows = conn.execute("""
        SELECT venue, surface, distance, COUNT(*) n,
               SUM(CASE WHEN pace_type='H' THEN 1 ELSE 0 END) h,
               SUM(CASE WHEN pace_type='M' THEN 1 ELSE 0 END) m,
               SUM(CASE WHEN pace_type='S' THEN 1 ELSE 0 END) s
        FROM race_laps
        WHERE pace_type IS NOT NULL
        GROUP BY venue, surface, distance
        HAVING n >= ?
        ORDER BY n DESC
        LIMIT ?
    """, (min_n, top_k)).fetchall()
    cells = []
    for venue, surface, distance, n, h, m, s in rows:
        cells.append({
            "venue": venue, "surface": surface, "distance": distance, "n": n,
            "real_h_rate": h / n, "real_m_rate": m / n, "real_s_rate": s / n,
        })
    return cells


DEFAULT_CORNER_R = 200.0  # r_entry/r_exit両方欠損時の最終フォールバック(JRA平均的なコーナー半径)


def _fill_missing_r(r_entry, r_exit):
    if r_entry is None and r_exit is None:
        return DEFAULT_CORNER_R, DEFAULT_CORNER_R
    if r_entry is None:
        return r_exit, r_exit
    if r_exit is None:
        return r_entry, r_entry
    return r_entry, r_exit


def get_course_geometry(conn, venue, surface, distance):
    """course_start_layout(d_c1)+course_layout(コーナー幾何)からPhase2用ジオメトリを構築。"""
    row = conn.execute("""
        SELECT venue_variant, d_c1_m, n_corners, start_in_chute FROM course_start_layout
        WHERE venue=? AND surface=? AND distance=?
    """, (venue, surface, distance)).fetchone()
    used_distance = distance
    fallback = False
    if row is None:
        rows = conn.execute("""
            SELECT venue_variant, distance, d_c1_m, n_corners, start_in_chute
            FROM course_start_layout WHERE venue=? AND surface=?
        """, (venue, surface)).fetchall()
        if not rows:
            return None
        rows = sorted(rows, key=lambda r: abs(r[1] - distance))
        variant, used_distance, d_c1, n_corners, start_in_chute = rows[0]
        fallback = True
    else:
        variant, d_c1, n_corners, start_in_chute = row
    is_chute = bool(start_in_chute)
    if d_c1 is None or (n_corners is not None and n_corners == 0):
        d_c1 = 0.0

    cl_rows = conn.execute("""
        SELECT venue_variant, circumference_m, straight_home_m, corner_no,
               r_entry_m, r_exit_m, arc_len_m
        FROM course_layout WHERE venue=? AND surface=? AND corner_no IN (1,2)
    """, (venue, surface)).fetchall()
    if not cl_rows:
        return {"d_c1": d_c1, "corner_zones": [], "circumference": None,
                "straight_home": None, "variant": variant, "used_distance": used_distance,
                "distance_fallback": fallback, "is_chute": is_chute}

    variants = {}
    for v, circ, sh, cno, re_, rx_, al in cl_rows:
        variants.setdefault(v, {})[cno] = (circ, sh, re_, rx_, al)
    chosen = variants.get(variant) or next(iter(variants.values()))
    if 1 not in chosen or 2 not in chosen:
        return {"d_c1": d_c1, "corner_zones": [], "circumference": None,
                "straight_home": None, "variant": variant, "used_distance": used_distance,
                "distance_fallback": fallback, "is_chute": is_chute}
    circ, straight_home, r1_entry, r1_exit, arc1 = chosen[1]
    _, _, r2_entry, r2_exit, arc2 = chosen[2]
    # 一部venue(福島ダ等)はr_entry_m/r_exit_mの片方がNULL(OSM解析で取得できなかった)。
    # 欠損側を他方の値で埋める(コーナー内で半径が一定というフォールバック近似)。
    r1_entry, r1_exit = _fill_missing_r(r1_entry, r1_exit)
    r2_entry, r2_exit = _fill_missing_r(r2_entry, r2_exit)
    zones = build_corner_zones(distance, circ, straight_home, arc1, arc2,
                                r1_entry, r1_exit, r2_entry, r2_exit)
    return {"d_c1": d_c1, "corner_zones": zones, "circumference": circ,
            "straight_home": straight_home, "variant": variant,
            "used_distance": used_distance, "distance_fallback": fallback, "is_chute": is_chute}


def get_slope_defs(conn, venue, surface):
    """course_slope(venue_elevation.md由来、2026-08-01追加)から坂の定義を取得。
    戻り値: [(remaining_start_m, remaining_end_m, grade), ...] (mc_dyn_engine.build_slope_zones
    にそのまま渡せる形式)。データがない会場は空リスト(=平坦として扱う)。"""
    rows = conn.execute("""
        SELECT remaining_start_m, remaining_end_m, grade FROM course_slope
        WHERE venue=? AND surface=?
    """, (venue, surface)).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def sample_real_field_compositions(conn, venue, surface, distance, max_races=500):
    """実際の(venue,surface,distance)のレースから、脚質構成(頭数×脚質カウント)を
    実測サンプリングする(Phase2仕様: '各セル50-100回、リーダーのラップ列を...'に
    使う疑似フィールドの生成元。ランダムな仮想馬ではなく実在した構成を使うことで
    現実的な逃げ馬数の分布を再現する)。
    【2026-08-05追加】ペース意図バイアス(pace_bias)用にクラス群("cls")も添付する。
    レース単位の属性追加のみで、構成リストの順序・サンプリングの乱数消費は不変
    (pace_bias_scale=0.0なら従来動作とビット単位一致)。"""
    rows = conn.execute("""
        SELECT race_id, pos4, num_horses, race_name FROM results
        WHERE venue=? AND surface=? AND distance=? AND pos4 IS NOT NULL
              AND num_horses > 1 AND finish < 90
    """, (venue, surface, distance)).fetchall()
    races = defaultdict(list)
    race_names = {}
    for race_id, pos4, n, rname in rows:
        races[race_id].append((pos4, n))
        if race_id not in race_names:
            race_names[race_id] = rname
    comps = []
    for race_id, lst in races.items():
        n = lst[0][1]
        counts = {"逃げ": 0, "先行": 0, "差し": 0, "追い込み": 0}
        for pos4, nh in lst:
            counts[classify_style_simple(pos4, nh)] += 1
        comps.append({"num_horses": n, "counts": counts,
                      "cls": pace_cls_group(race_names.get(race_id))})
    if max_races and len(comps) > max_races:
        # 【2026-08-01修正】共有のグローバルrandom.sample()を使うと、このセルより前に
        # 呼ばれたsample_real_field_compositions()の回数(座標降下法の試行回数など、
        # 呼び出し順序に依存する)によってどの500件が抽出されるかが変わってしまい、
        # 同じパラメータでも呼び出し順序次第でゲート3のような僅差指標がブレる不具合が
        # あった。セル固有(venue/surface/distance)かつ再現可能なローカルRNGに切替え。
        # 組み込みhash()は文字列に対してプロセスごとにランダム化される(PYTHONHASHSEED)ため
        # 使わない(過去にv6 BTの非決定性の原因になった実例あり)。hashlibで安定なシードを作る。
        import hashlib
        key = f"{venue}|{surface}|{distance}".encode("utf-8")
        cell_seed = int(hashlib.md5(key).hexdigest()[:8], 16)
        comps = random.Random(cell_seed).sample(comps, max_races)
    return comps


def _styles_from_counts(counts):
    styles = []
    for style, c in counts.items():
        styles.extend([style] * c)
    return styles


# ============================================================================
# セル単位シミュレーション実行
# ============================================================================

def run_cell(conn, cell, params, q_star, n_sim=60, dt=0.5, seed_base=0, horse_noise_sd=3.0):
    venue, surface, distance = cell["venue"], cell["surface"], cell["distance"]
    par_time = get_par_time(conn, venue, surface, distance)
    if par_time is None:
        return None, "par_time取得不可"
    geometry = get_course_geometry(conn, venue, surface, distance)
    if geometry is None:
        return None, "course_start_layout/course_layoutデータなし"

    # 【2026-08-01追加】坂(venue_elevation.md由来、course_slopeテーブル)。v_baseの
    # par自動アンカーにも坂を含める — 坂を含めずにアンカーすると、実際のpar_time
    # (坂込みの実測タイム)に対しv_baseが坂の減速分を織り込まずに解かれてしまい、
    # simulate_field側で坂を追加した分だけ総タイムが実際よりさらに遅くなる
    # (アンカーとフィールドシミュレーションで坂の扱いに矛盾が生じる)ため。
    slope_defs = get_slope_defs(conn, venue, surface)
    slope_zones = build_slope_zones(distance, slope_defs)
    k_slope = params.get("k_slope", K_SLOPE)

    v_base, _ = anchor_v_base(distance, par_time, k0=params["k0"], phi_fade=params["phi_fade"],
                               accel_frac=params["accel_frac"],
                               slope_zones=slope_zones, k_slope=k_slope)
    comps = sample_real_field_compositions(conn, venue, surface, distance)
    if not comps:
        return None, "実測フィールド構成が取得できない(該当レースなし)"

    # 【2026-08-01追加】is_chute_geom(構造上の引込線発走か否か)とapply_chute_boost
    # (chute_dash_fracのダッシュ強制を実際に適用するか)を分離する。実測診断で、
    # 「引込線発走→極端なHペース」効果は新潟ダ1200/中京ダ1400/阪神ダ1400等ダート
    # 短距離で頑健に確認済みだが、同じ機構を芝の引込線発走(東京芝1600m等)にも
    # 一律適用すると実測(H率一桁台〜30%台)を大幅に超過するシミュH率(65-85%)に
    # なる不具合を確認した。ダート限定に絞ることでgate1相関が0.654->0.671に改善し、
    # gate2(新潟ダ1200)には影響しないことを確認済み。gate3(単騎逃げ vs 複数逃げ)の
    # 母集団除外は構造上の引込線発走(is_chute_geom)基準のまま変更しない — ダッシュ
    # 強制の有無に関わらず引込線発走という発走地点の構造自体がdash_min_fracの検証
    # 対象外であることに変わりはないため。
    is_chute_geom = bool(geometry.get("is_chute", False))
    apply_chute_boost = is_chute_geom and surface == "ダ"

    # 【2026-08-01追加】kappa_press(位置取り競合の圧力)を芝/ダートで別値にする。
    # 実測診断: 表面をプールした単一グローバル値では、ダートは終始Hペース側に
    # バイアスが不足(kappa_press=0.3でH率-10.9pt過小評価)し、芝は逆にH側へ
    # 過大評価(同+15.5pt)する綱引きが発生していた。kappa_pressを振ると両者が
    # 「同じ方向」に動く(上げるとダートのバイアスは解消に向かうが芝はさらに悪化)
    # ため単一値では両立不可能と判明。ダート≈0.85でバイアスほぼ0、芝は0.05まで
    # 下げてもバイアス+9.5pt残るためkappa_press単体では芝を完全には説明できない
    # (物理モデルの別の要因、次フェーズの調査課題)が、表面別に分けるだけで
    # ゲート1相関は0.691->0.786まで改善することを確認済み。
    kappa_press = params.get("kappa_press_dirt", params["kappa_press"]) if surface == "ダ" \
        else params.get("kappa_press_turf", params["kappa_press"])

    # 【2026-08-01追加】kappa_press/dash_min_fracを表面分離してもなお芝に残っていた
    # +9.15ptの床を切り分けたところ、D_STYLE(脚質別ダッシュ力)が主因と判明
    # (d_scale=0で符号が逆転(-6.96%)まで動く一方、a_lat/rho_saveはほぼ無効)。
    # 芝だけD_SCALEを部分的に弱める(完全ゼロ化は相関自体を悪化させるため不採用)。
    d_scale = 1.0 if surface == "ダ" else params.get("d_scale_turf", D_SCALE_TURF)

    # 【2026-08-04追加】構成依存イージング(単騎逃げの余裕)。kappa_press/d_scaleと同じく
    # 表面別に選択する(芝は実測の単騎逃げS率47.1%/複数20.0%と強く、ダートは8.1%/2.7%と
    # 弱いため必要な強度が大きく異なる)。mc_dyn_engine.pyのSOLO_EASE_SCALE既定値は0.0
    # (レガシー互換)なので、paramsに値が無ければ従来動作のまま。
    solo_ease_scale = params.get("solo_ease_scale_dirt" if surface == "ダ"
                                 else "solo_ease_scale_turf", SOLO_EASE_SCALE)
    ease_rival_sat = params.get("ease_rival_sat", EASE_RIVAL_SAT)

    # 【2026-08-07追加】複数逃げの先導権決着(レースレベルの戦術的裁量)。表面別選択は
    # kappa_press等と同一方式。既定0.0でレガシー互換(mc_dyn_engine.NIGE_SETTLE_PROB参照)。
    nige_settle_prob = params.get("nige_settle_prob_dirt" if surface == "ダ"
                                  else "nige_settle_prob_turf", 0.0)

    # 【2026-08-05追加】ペース意図バイアス(クラス・頭数・直線長 → 先頭馬巡航シフト)。
    # pace_bias_scale=0.0(レガシー)で完全無効。係数の出典はmc_dyn_engine.pyの
    # PACE_BIAS_*(訓練会場=東京/中山/京都/小倉/福島の実データ回帰から導出、
    # 検証会場=函館/札幌/阪神/中京は係数決定に不使用)。
    pace_bias_scale = params.get("pace_bias_scale", 0.0)
    straight_home_m = geometry.get("straight_home")
    # コーナー無しコース(新潟芝1000等の直線競走)は新機構(pace_bias/dash窓固定)の
    # 対象外(レガシー挙動)。コーナー前提の回帰・設計を直線競走に外挿しない一般則。
    has_corners = bool(geometry["corner_zones"])

    rng = random.Random(seed_base)
    counts_h = counts_m = counts_s = counts_none = 0
    records = []
    for i in range(n_sim):
        comp = comps[rng.randrange(len(comps))]
        styles = _styles_from_counts(comp["counts"])
        if len(styles) < 2:
            continue
        pace_bias = pace_bias_scale * pace_bias_for(
            surface, comp.get("cls"), comp.get("num_horses"), straight_home_m,
            distance=distance, has_corners=has_corners) \
            if pace_bias_scale else 0.0
        horses = [{"style": st,
                   "spd": 80.0 + rng.gauss(0, horse_noise_sd),
                   "spr": 80.0 + rng.gauss(0, horse_noise_sd),
                   "sta": 75.0 + rng.gauss(0, horse_noise_sd)} for st in styles]
        result = simulate_field(
            distance, v_base, geometry["d_c1"], geometry["corner_zones"], horses, q_star,
            k0=params["k0"], phi_fade=params["phi_fade"],
            kappa_press=kappa_press, k_gap=params.get("k_gap", K_GAP),
            rho_save=params["rho_save"], a_lat=params["a_lat"],
            dash_min_frac=params.get("dash_min_frac", DASH_MIN_FRAC),
            dash_rival_sat=params.get("dash_rival_sat", DASH_RIVAL_SAT),
            is_chute_start=apply_chute_boost,
            chute_dash_frac=params.get("chute_dash_frac", CHUTE_DASH_FRAC),
            d_scale=d_scale,
            solo_ease_scale=solo_ease_scale, ease_rival_sat=ease_rival_sat,
            nige_settle_prob=nige_settle_prob,
            # レースレベルのペース意図ノイズ(2026-08-05追加、既定0.0=レガシー互換)。
            # kappa_press等と同じ表面分離(実測stdの再現に必要な量が芝0.7/ダ0.9と異なる、
            # 表面別のstd検証はB4スイープ参照)。単一キーpace_noise_sigmaはフォールバック。
            pace_noise_sigma=params.get(
                "pace_noise_sigma_dirt" if surface == "ダ" else "pace_noise_sigma_turf",
                params.get("pace_noise_sigma", 0.0)),
            pace_bias=pace_bias,
            # dash_cap_m: paramsに明示指定があればそれを使い(スイープ・レガシー再現用)、
            # なければ距離テーパー(dash_cap_for、2026-08-04採用)を適用する。
            dash_cap_m=params.get("dash_cap_m") or dash_cap_for(surface, distance),
            # 芝ダッシュ窓のd_c1非依存化(2026-08-05追加)。False(既定)でレガシー。
            # コーナー無しコース(直線競走)は対象外(has_corners参照)。
            dash_window_m=(dash_window_for(surface, distance)
                           if params.get("dash_window_turf_fixed") and has_corners else None),
            # 【検証済み・不採用 2026-08-05】仕掛け長の距離比例化(kick_start=min(800,
            # frac*distance)、芝のみ)を1600m単騎S過大の対策としてfrac={0.5,0.45,0.42}で
            # スイープしたが、gate1が0.906→0.845-0.847に崩壊(芝短距離のH構造を破壊)し
            # 3aもほぼ不動(+33.6→+31.6)のため採用しない。再挑戦時はこの記録を参照。
            slope_zones=slope_zones, k_slope=k_slope,
            dt=dt, seed=seed_base * 100003 + i,
        )
        derived = calc_derived(result["leader_laps"], distance)
        pt = derived.get("pace_type")
        nige_count = comp["counts"].get("逃げ", 0)
        records.append({"pace_type": pt, "nige_count": nige_count,
                         "is_chute": is_chute_geom})
        if pt == "H":
            counts_h += 1
        elif pt == "M":
            counts_m += 1
        elif pt == "S":
            counts_s += 1
        else:
            counts_none += 1

    total = counts_h + counts_m + counts_s
    if total == 0:
        return None, "全シミュでpace_type判定不可(ラップ本数不足)"

    return {
        "venue": venue, "surface": surface, "distance": distance,
        "n_real": cell["n"],
        "real_h_rate": cell["real_h_rate"], "real_m_rate": cell["real_m_rate"],
        "real_s_rate": cell["real_s_rate"],
        "sim_h_rate": counts_h / total, "sim_m_rate": counts_m / total, "sim_s_rate": counts_s / total,
        "n_sim_valid": total, "n_sim_none": counts_none,
        "d_c1": geometry["d_c1"], "n_corner_zones": len(geometry["corner_zones"]),
        "records": records,
    }, None


def evaluate(conn, params, cells, q_star, n_sim=60, dt=0.5, verbose=True):
    results = []
    for cell in cells:
        r, err = run_cell(conn, cell, params, q_star, n_sim=n_sim, dt=dt)
        label = f"{cell['venue']}{cell['surface']}{cell['distance']}m"
        if r is None:
            if verbose:
                print(f"  SKIP {label}: {err}")
            continue
        results.append(r)
        if verbose:
            print(f"  {label:16s} n_real={cell['n']:5d}  "
                  f"実測H/M/S={cell['real_h_rate']*100:5.1f}/{cell['real_m_rate']*100:5.1f}/{cell['real_s_rate']*100:5.1f}%  "
                  f"シミュH/M/S={r['sim_h_rate']*100:5.1f}/{r['sim_m_rate']*100:5.1f}/{r['sim_s_rate']*100:5.1f}%")
    return results


# ============================================================================
# ゲート計算
# ============================================================================

def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return 0.0
    return cov / math.sqrt(vx * vy)


def gate1_correlation(results):
    xs = [r["real_h_rate"] for r in results]
    ys = [r["sim_h_rate"] for r in results]
    return pearson(xs, ys), len(results)


def gate2_niigata(results):
    for r in results:
        if (r["venue"], r["surface"], r["distance"]) == NIIGATA_DA1200:
            return r["sim_h_rate"]
    return None


def gate3_nige_ordering(results, exclude_chute=True):
    """単騎逃げ vs 複数逃げのS率比較。【2026-08-01修正】引込線発走(is_chute)は
    chute_dash_fracという独立機構で「ライバル数によらずダッシュ強制」を表現しており、
    dash_min_frac(ライバル数依存のスケール)が検証したい対象と別物。実測診断の結果、
    引込線発走セルだけ見ると単騎逃げS率(1.1%)が複数逃げS率(1.7%)を下回り符号が
    逆転しており(chute_dash_frac=1.0で単騎でもフルダッシュ扱いになるため)、これを
    混ぜるとゲート3本来の健全な差(非引込線のみ: +0.76〜0.79pt)がノイズで相殺され
    僅差(3.6%>3.5%)になっていた。デフォルトで引込線発走セルを除外する。"""
    single, multi = [], []
    for r in results:
        for rec in r["records"]:
            if rec["pace_type"] is None:
                continue
            if exclude_chute and rec.get("is_chute"):
                continue
            is_s = 1 if rec["pace_type"] == "S" else 0
            if rec["nige_count"] == 1:
                single.append(is_s)
            elif rec["nige_count"] >= 2:
                multi.append(is_s)
    s_rate_single = sum(single) / len(single) if single else None
    s_rate_multi = sum(multi) / len(multi) if multi else None
    return s_rate_single, s_rate_multi, len(single), len(multi)


# --- ゲート3 新基準(2026-08-04、絶対水準版) -----------------------------------
# 【経緯】従来の「単騎S% > 複数S%(方向のみ、全表面プール)」は、S質量がほぼ生成されない
# 状態(レガシー: 4.7% vs 3.5%、差1.2pt)でも合格してしまう甘い基準だった。実測では
# 単騎逃げ→スローの効果は芝で差27.3pt(レースプール)/19.4pt(セル等重み)と桁違いに
# 大きく、ダートは5.8pt/3.8ptと小さい(いずれもtop60セル・引込線発走セル除外・
# 2026-08-04時点のkeiba.db実測)。表面で必要な水準が大きく異なるため表面別に分離する。
# 【閾値の導出(sim値への後付けではなく実測から設計)】
#   3a 芝効果量: シミュ芝プールの単騎S%-複数S% >= 10pt。
#      実測リファレンスのうち保守的な方(セル等重み19.4pt)の半分= 9.7pt を丸めた値。
#      「実効果の半分未満しか捕捉しないモデルを落とす」バー。レガシーは差1pt未満で明確にNG。
#   3b 芝絶対水準: |芝セル平均シミュS率 - 芝セル平均実測S率| <= 10pt。
#      実測水準30.5%の約1/3。S質量そのものの欠落(方向だけ合って絶対値が桁違い)を検知する。
#      こちらは引込線含む全芝セルのセル平均(sim_s_rate/real_s_rateはセル単位で対応が取れるため)。
#   3c ダート方向性: シミュダプールの単騎S% >= 複数S% - 1.0pt(サンプリングノイズ許容、
#      群n≈500-900・S率1%前後での2se≈1.2ptに基づく)。ダートのS生成機構は未実装のため
#      効果量基準は現段階では課さない — 機構が入った時点で実測差(5.8pt)の半分≈3ptへ
#      引き上げること。
GATE3A_TURF_DIFF_MIN = 0.10   # 3a: 芝 単騎-複数 S率差の下限
GATE3B_TURF_BIAS_MAX = 0.10   # 3b: 芝セル平均S率バイアスの上限(絶対値)
GATE3C_DIRT_TOL = 0.01        # 3c: ダート方向性のノイズ許容


def gate3_stats(results, exclude_chute=True):
    """新ゲート3用の表面別統計。戻り値dict:
    turf_s_single/turf_s_multi/turf_n_single/turf_n_multi (芝プール、chute除外),
    dirt_s_single/dirt_s_multi/dirt_n_single/dirt_n_multi (ダプール、chute除外),
    turf_sim_s_mean/turf_real_s_mean/turf_bias (芝セル平均、全セル),
    legacy_s_single/legacy_s_multi (旧基準の全表面プール、参考表示用)"""
    pool = {"芝": {"s1": 0, "n1": 0, "s2": 0, "n2": 0},
            "ダ": {"s1": 0, "n1": 0, "s2": 0, "n2": 0}}
    turf_sim_s, turf_real_s = [], []
    for r in results:
        surf = r["surface"]
        if surf == "芝":
            turf_sim_s.append(r["sim_s_rate"])
            turf_real_s.append(r["real_s_rate"])
        if surf not in pool:
            continue
        for rec in r["records"]:
            if rec["pace_type"] is None:
                continue
            if exclude_chute and rec.get("is_chute"):
                continue
            is_s = 1 if rec["pace_type"] == "S" else 0
            if rec["nige_count"] == 1:
                pool[surf]["n1"] += 1
                pool[surf]["s1"] += is_s
            elif rec["nige_count"] >= 2:
                pool[surf]["n2"] += 1
                pool[surf]["s2"] += is_s
    out = {}
    for surf, key in [("芝", "turf"), ("ダ", "dirt")]:
        p = pool[surf]
        out[f"{key}_s_single"] = p["s1"] / p["n1"] if p["n1"] else None
        out[f"{key}_s_multi"] = p["s2"] / p["n2"] if p["n2"] else None
        out[f"{key}_n_single"] = p["n1"]
        out[f"{key}_n_multi"] = p["n2"]
    out["turf_sim_s_mean"] = sum(turf_sim_s) / len(turf_sim_s) if turf_sim_s else None
    out["turf_real_s_mean"] = sum(turf_real_s) / len(turf_real_s) if turf_real_s else None
    out["turf_bias"] = (out["turf_sim_s_mean"] - out["turf_real_s_mean"]
                        if turf_sim_s else None)
    legacy_s1, legacy_s2, _, _ = gate3_nige_ordering(results, exclude_chute=exclude_chute)
    out["legacy_s_single"], out["legacy_s_multi"] = legacy_s1, legacy_s2
    return out


def gate3_pass(g3):
    """新ゲート3の合否判定(3a/3b/3cの個別bool + 総合)。データ不足の項目はNG扱い。"""
    ok_a = (g3["turf_s_single"] is not None and g3["turf_s_multi"] is not None
            and g3["turf_s_single"] - g3["turf_s_multi"] >= GATE3A_TURF_DIFF_MIN)
    ok_b = g3["turf_bias"] is not None and abs(g3["turf_bias"]) <= GATE3B_TURF_BIAS_MAX
    ok_c = (g3["dirt_s_single"] is not None and g3["dirt_s_multi"] is not None
            and g3["dirt_s_single"] >= g3["dirt_s_multi"] - GATE3C_DIRT_TOL)
    return ok_a, ok_b, ok_c, (ok_a and ok_b and ok_c)


def print_gates(results, tag=""):
    corr, n_cells = gate1_correlation(results)
    niigata_h = gate2_niigata(results)
    g3 = gate3_stats(results)
    ok_a, ok_b, ok_c, ok_all = gate3_pass(g3)
    print(f"\n{'='*70}\nゲート判定 {tag}\n{'='*70}")
    print(f"ゲート1: 上位{n_cells}セルの実測H率 vs シミュH率 相関 = {corr:.3f}  "
          f"{'OK(>=0.7)' if corr >= 0.7 else 'NG'}")
    if niigata_h is not None:
        print(f"ゲート2: 新潟ダ1200 シミュH率 = {niigata_h*100:.1f}%  "
              f"{'OK(>=90%)' if niigata_h >= 0.90 else 'NG'}")
    else:
        print("ゲート2: 新潟ダ1200 データなし(SKIP対象だった可能性)")
    # ゲート3(2026-08-04新基準: 表面別・絶対水準。導出はGATE3A/3B/3C定数のコメント参照)
    if g3["turf_s_single"] is not None and g3["turf_s_multi"] is not None:
        diff = g3["turf_s_single"] - g3["turf_s_multi"]
        print(f"ゲート3a(芝効果量): 単騎S={g3['turf_s_single']*100:.1f}%(n={g3['turf_n_single']}) - "
              f"複数S={g3['turf_s_multi']*100:.1f}%(n={g3['turf_n_multi']}) = {diff*100:+.1f}pt  "
              f"{'OK' if ok_a else 'NG'}(>= {GATE3A_TURF_DIFF_MIN*100:.0f}pt、実測19.4-27.3pt)")
    else:
        print("ゲート3a: 芝サンプル不足 NG")
    if g3["turf_bias"] is not None:
        print(f"ゲート3b(芝絶対水準): 芝セル平均S率 シミュ{g3['turf_sim_s_mean']*100:.1f}% vs "
              f"実測{g3['turf_real_s_mean']*100:.1f}% (バイアス{g3['turf_bias']*100:+.1f}pt)  "
              f"{'OK' if ok_b else 'NG'}(|バイアス|<= {GATE3B_TURF_BIAS_MAX*100:.0f}pt)")
    else:
        print("ゲート3b: 芝セルなし NG")
    if g3["dirt_s_single"] is not None and g3["dirt_s_multi"] is not None:
        print(f"ゲート3c(ダ方向性): 単騎S={g3['dirt_s_single']*100:.1f}%(n={g3['dirt_n_single']}) vs "
              f"複数S={g3['dirt_s_multi']*100:.1f}%(n={g3['dirt_n_multi']})  "
              f"{'OK' if ok_c else 'NG'}(単騎>=複数-{GATE3C_DIRT_TOL*100:.0f}pt。S生成機構未実装のため方向のみ、"
              f"機構実装後は実測差5.8ptの半分へ引き上げ)")
    else:
        print("ゲート3c: ダサンプル不足 NG")
    print(f"ゲート3総合: {'OK' if ok_all else 'NG'}  "
          f"(参考・旧基準の全表面プール: 単騎{(g3['legacy_s_single'] or 0)*100:.1f}% vs "
          f"複数{(g3['legacy_s_multi'] or 0)*100:.1f}%)")
    return {
        "gate1_corr": corr, "gate1_n_cells": n_cells,
        "gate2_niigata_h_rate": niigata_h,
        # 旧キー(JSON継続性のため残す。値は旧基準=全表面プール)
        "gate3_s_single": g3["legacy_s_single"], "gate3_s_multi": g3["legacy_s_multi"],
        # 新基準(2026-08-04)
        "gate3_turf_s_single": g3["turf_s_single"], "gate3_turf_s_multi": g3["turf_s_multi"],
        "gate3_turf_n_single": g3["turf_n_single"], "gate3_turf_n_multi": g3["turf_n_multi"],
        "gate3_turf_sim_s_mean": g3["turf_sim_s_mean"],
        "gate3_turf_real_s_mean": g3["turf_real_s_mean"],
        "gate3_turf_bias": g3["turf_bias"],
        "gate3_dirt_s_single": g3["dirt_s_single"], "gate3_dirt_s_multi": g3["dirt_s_multi"],
        "gate3_pass_a": ok_a, "gate3_pass_b": ok_b, "gate3_pass_c": ok_c,
        "gate3_pass": ok_all,
    }


# ============================================================================
# 軽量座標降下法(kappa_press / rho_save / a_lat のみ、Phase1生理パラメータは不変)
# ============================================================================

CALIB_CANDIDATES = {
    # 【2026-08-01変更】単一のkappa_pressを表面別(dirt/turf)に分離。実測診断で、
    # プールした単一値ではダート(Hペース過小評価)と芝(過大評価)が同じ方向に
    # しか動かせず両立不可能と判明したため(run_cell()のコメント参照)。
    "kappa_press_dirt": [0.5, 0.7, 0.85, 1.0, 1.2, 1.5],
    "kappa_press_turf": [0.0, 0.02, 0.05, 0.1, 0.2],
    "rho_save": [0.15, 0.3, 0.5, 0.8, 1.0, 1.3],
    "a_lat": [1.6, 1.9, 2.1, 2.5, 3.0],
    "dash_min_frac": [0.15, 0.25, 0.4, 0.55, 0.7, 0.85],
    "dash_rival_sat": [1.0, 2.0, 3.0, 4.0],
    "chute_dash_frac": [0.6, 0.7, 0.8, 0.85, 0.9, 1.0],
    "d_scale_turf": [0.55, 0.7, 0.85, 1.0],
    "k_slope": [0.0, 5.0, 10.0, 20.0, 30.0, 45.0],
    # 構成依存イージング(2026-08-04追加、単騎逃げ→スローペースの再現用)
    "solo_ease_scale_turf": [0.0, 0.4, 0.55, 0.7, 0.85, 1.0],
    "solo_ease_scale_dirt": [0.0, 0.1, 0.2, 0.3],
    "ease_rival_sat": [4.0, 8.0, 12.0],
    # P0ダッシュ窓キャップ(2026-08-04パラメータ化。従来は400ハードコード)
    "dash_cap_m": [250.0, 300.0, 350.0, 400.0],
    # レースレベルのペース意図ノイズ(2026-08-05追加。実測balance stdの再現が本来の
    # 較正基準なのでB4スイープを優先し、座標降下法ではゲート指標を壊さない範囲の微調整のみ)
    "pace_noise_sigma_turf": [0.0, 0.5, 0.7, 0.9],
    "pace_noise_sigma_dirt": [0.0, 0.5, 0.7, 0.9, 1.1],
}


def _score_for_calib(conn, params, calib_cells, q_star, n_sim, dt):
    """較正用の軽量スコア: ゲート1相関 + ゲート2(新潟ダ1200 H率が90%にどれだけ近いか)
    + ゲート3(単騎逃げS率が複数逃げS率をどれだけ上回るか)を単純合算した目的関数
    (高いほど良い)。【2026-08-01追加】ゲート3を objective に含めていなかったため、
    ゲート1改善の較正がゲート3を犠牲にする綱引きが発生した(dash_min_frac低下で
    ゲート1=0.704合格するもゲート3が逆転)。3ゲート同時最適化に修正。"""
    results = evaluate(conn, params, calib_cells, q_star, n_sim=n_sim, dt=dt, verbose=False)
    if not results:
        return -999.0, results
    corr, _ = gate1_correlation(results)
    niigata_h = gate2_niigata(results)
    niigata_score = -abs((niigata_h or 0.0) - 0.90)
    # 【2026-08-04変更】ゲート3の新基準(表面別・絶対水準)に合わせてスコアも再設計:
    #  - 芝効果量: 実測セル等重み差(19.4pt)でキャップ — 実効果を超えて差を伸ばしても加点しない
    #    (旧スコアはキャップなしで、差を過剰に伸ばす方向へ較正が暴走し得た)
    #  - 芝絶対水準: セル平均バイアスをペナルティ化
    #  - ダ方向性: 許容(1pt)を超える逆転のみペナルティ
    g3 = gate3_stats(results)
    gate3_score = 0.0
    if g3["turf_s_single"] is not None and g3["turf_s_multi"] is not None:
        gate3_score += 2.5 * min(g3["turf_s_single"] - g3["turf_s_multi"], 0.194)
    if g3["turf_bias"] is not None:
        gate3_score -= 2.5 * abs(g3["turf_bias"])
    if g3["dirt_s_single"] is not None and g3["dirt_s_multi"] is not None:
        gate3_score -= 2.5 * max(0.0, (g3["dirt_s_multi"] - g3["dirt_s_single"]) - GATE3C_DIRT_TOL)
    return corr + niigata_score + gate3_score, results


def calibrate(conn, base_params, calib_cells, q_star, n_sim=25, dt=0.5):
    best = dict(base_params)
    best_score, _ = _score_for_calib(conn, best, calib_cells, q_star, n_sim, dt)
    print(f"較正初期スコア={best_score:.4f}  params={ {k: best[k] for k in CALIB_CANDIDATES} }")
    for rd in range(2):
        improved = False
        for param, candidates in CALIB_CANDIDATES.items():
            local_best_val, local_best_score = best[param], best_score
            for cand in candidates:
                trial = dict(best)
                trial[param] = cand
                score, _ = _score_for_calib(conn, trial, calib_cells, q_star, n_sim, dt)
                if score > local_best_score:
                    local_best_score, local_best_val = score, cand
            if local_best_val != best[param]:
                best[param] = local_best_val
                best_score = local_best_score
                improved = True
                print(f"  round{rd+1} {param} -> {local_best_val} (score={best_score:.4f})")
        if not improved:
            print(f"  round{rd+1}: 変化なし、収束")
            break
    return best, best_score


# ============================================================================
# main
# ============================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="動作確認用の小規模実行")
    ap.add_argument("--calibrate", action="store_true", help="座標降下法でkappa_press/rho_save/a_latを調整")
    ap.add_argument("--n-sim", type=int, default=None, help="セルあたりのシミュ回数を上書き")
    ap.add_argument("--top-k", type=int, default=None, help="対象セル数を上書き")
    ap.add_argument("--dt", type=float, default=0.5, help="時間刻み(秒)")
    ap.add_argument("--kappa-press", type=float, default=None, help="kappa_pressを直接指定(較正をスキップ)")
    ap.add_argument("--rho-save", type=float, default=None, help="rho_saveを直接指定(較正をスキップ)")
    ap.add_argument("--a-lat", type=float, default=None, help="a_latを直接指定(較正をスキップ)")
    ap.add_argument("--global-seed", type=int, default=20260801,
                     help="実測フィールド構成サンプリング(random.sample)の再現性用シード")
    args = ap.parse_args()
    random.seed(args.global_seed)

    t0 = time.time()
    conn = __import__("sqlite3").connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")

    phase1_params = load_phase1_params()
    params = {
        "accel_frac": phase1_params["accel_frac"],
        "k0": phase1_params["k0"],
        "phi_fade": phase1_params["phi_fade"],
        "kappa_press": KAPPA_PRESS,
        "kappa_press_dirt": KAPPA_PRESS_DIRT,
        "kappa_press_turf": KAPPA_PRESS_TURF,
        "rho_save": RHO_SAVE,
        "a_lat": A_LAT,
        "k_gap": K_GAP,
        "dash_min_frac": DASH_MIN_FRAC,
        "dash_rival_sat": DASH_RIVAL_SAT,
        "chute_dash_frac": CHUTE_DASH_FRAC,
        "d_scale_turf": D_SCALE_TURF,
        "k_slope": K_SLOPE,
        # 構成依存イージング(2026-08-04追加・較正済み)。値の出典はmc_dyn_engine.pyの
        # SOLO_EASE_SCALE_TURF/DIRT(較正するたびにエンジン側定数を更新すること —
        # 較正結果をJSON保存だけして既定値に反映し忘れる過去の不具合の再発防止)。
        "solo_ease_scale_turf": SOLO_EASE_SCALE_TURF,
        "solo_ease_scale_dirt": SOLO_EASE_SCALE_DIRT,
        "ease_rival_sat": EASE_RIVAL_SAT,
        # 複数逃げの先導権決着(2026-08-07追加・較正値はエンジン側定数が正)
        "nige_settle_prob_turf": NIGE_SETTLE_PROB_TURF,
        "nige_settle_prob_dirt": NIGE_SETTLE_PROB_DIRT,
        # None = 距離テーパー(dash_cap_for)を適用(2026-08-04採用の既定)。
        # 数値を入れると全セル一律のキャップになる(スイープ・レガシー再現用)。
        "dash_cap_m": None,
        # レースレベルのペース意図ノイズ(2026-08-05追加・較正済み)。値の出典は
        # mc_dyn_engine.pyのPACE_NOISE_SIGMA_TURF/DIRT(較正のたびにエンジン側定数を
        # 更新すること — JSON保存だけして既定値に反映し忘れる過去の不具合の再発防止)。
        "pace_noise_sigma_turf": PACE_NOISE_SIGMA_TURF,
        "pace_noise_sigma_dirt": PACE_NOISE_SIGMA_DIRT,
        # レース属性ペース意図バイアス(2026-08-05追加・採用)。1.0=有効(係数は
        # mc_dyn_engine.pyのPACE_BIAS_*)、0.0でレガシー再現。
        "pace_bias_scale": 1.0,
        # 芝dash窓のd_c1非依存化(2026-08-05採用)。False+pace_bias_scale=0+
        # ease_rival_sat=4.0で旧状態(gate1=0.904のB修正時点)を再現できる。
        "dash_window_turf_fixed": True,
    }
    if args.kappa_press is not None:
        params["kappa_press"] = args.kappa_press
        params["kappa_press_dirt"] = args.kappa_press
        params["kappa_press_turf"] = args.kappa_press
    if args.rho_save is not None:
        params["rho_save"] = args.rho_save
    if args.a_lat is not None:
        params["a_lat"] = args.a_lat
    print(f"Phase1パラメータ(不変): accel_frac={params['accel_frac']} k0={params['k0']} phi_fade={params['phi_fade']}")
    print(f"Phase2初期パラメータ: kappa_press_dirt={params['kappa_press_dirt']} kappa_press_turf={params['kappa_press_turf']} "
          f"rho_save={params['rho_save']} a_lat={params['a_lat']} "
          f"dash_min_frac={params['dash_min_frac']} dash_rival_sat={params['dash_rival_sat']}")

    q_star, q_star_n = compute_q_star(conn)
    print(f"\nq_star(脚質別pos1/num_horses中央値): " +
          ", ".join(f"{s}={v:.3f}(n={q_star_n[s]})" for s, v in q_star.items()))

    top_k = args.top_k or (10 if args.quick else 60)
    n_sim = args.n_sim or (15 if args.quick else 60)
    min_n = 30 if args.quick else 100

    cells = get_target_cells(conn, min_n=min_n, top_k=top_k)
    print(f"\n対象セル数: {len(cells)} (min_n={min_n}, top_k={top_k}, n_sim={n_sim}, dt={args.dt})")

    if args.calibrate:
        # 較正セルはダート/芝を層化サンプリングする(nの多い順だけで選ぶとダートに
        # 偏り、実際に芝中距離戦でシミュがH寄りに偏る系統的なズレを較正が全く
        # 見られないまま終わってしまう不具合を確認したため)。
        da_cells = [c for c in cells if c["surface"] == "ダ"][:8]
        shiba_cells = [c for c in cells if c["surface"] == "芝"][:8]
        calib_cells = da_cells + shiba_cells
        if not any((c["venue"], c["surface"], c["distance"]) == NIIGATA_DA1200 for c in calib_cells):
            niigata_cell = next((c for c in get_target_cells(conn, min_n=30, top_k=200)
                                  if (c["venue"], c["surface"], c["distance"]) == NIIGATA_DA1200), None)
            if niigata_cell:
                calib_cells.append(niigata_cell)
        print(f"\n較正対象セル: {len(calib_cells)}件(ダート{len(da_cells)}+芝{len(shiba_cells)}+新潟ダ1200)")
        params, best_score = calibrate(conn, params, calib_cells, q_star, n_sim=20, dt=args.dt)
        print(f"\n較正後パラメータ: kappa_press_dirt={params['kappa_press_dirt']} kappa_press_turf={params['kappa_press_turf']} "
              f"rho_save={params['rho_save']} a_lat={params['a_lat']} "
              f"dash_min_frac={params['dash_min_frac']} dash_rival_sat={params['dash_rival_sat']}")

    print(f"\n{'='*70}\nフル検証実行\n{'='*70}")
    results = evaluate(conn, params, cells, q_star, n_sim=n_sim, dt=args.dt, verbose=True)
    gates = print_gates(results, tag="(最終)")

    elapsed = time.time() - t0
    print(f"\n所要時間: {elapsed:.1f}秒")

    out_params = dict(params)
    out_params["q_star"] = q_star
    with open(PHASE2_PARAMS_PATH, "w", encoding="utf-8") as f:
        json.dump({"params": out_params, "gates": gates, "n_cells": len(results),
                    "n_sim": n_sim, "dt": args.dt}, f, ensure_ascii=False, indent=2)

    verdict_out = []
    for r in results:
        r2 = dict(r)
        r2.pop("records")  # 詳細レコードは verdict には保存しない(容量節約)
        verdict_out.append(r2)
    with open(PHASE2_VERDICT_PATH, "w", encoding="utf-8") as f:
        json.dump({"cells": verdict_out, "gates": gates}, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n保存: {PHASE2_PARAMS_PATH}, {PHASE2_VERDICT_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
