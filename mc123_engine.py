"""
mc123_engine.py — MCエンジン本体 (Phase 2)

run_mc_fixed(generate_mc_record.py)・run_mc_lite(backtest_sim_lite.py)の複製ではなく、
CTA(build_class_par.py)・個体化ペース応答/Grit(build_pace_baseline.py)・風スロットを
組み込んだ完全新規の独立エンジン。馬ごとに{p1,p2,p3,ptop3}を出力する。

【まだ本番未統合の独立ライン】v6.6への統合・表示ページ(mc123.html)・オッズフィルタ・
正式BT/WF CVはすべてスコープ外(Phase 2は「動く版」を作ることが目的)。

風スロット係数(a, b)は race_wind_v2 x results の簡易回帰で概算した値であり、
厳密な年次WF較正(Gate 1以降)はまだ行っていない。プレースホルダとして明記する。
"""
import hashlib
import sqlite3
import numpy as np

import generate_race_sim as gsim
from build_class_par import (
    build_class_par_table, calibrate_k_cls, load_same_day_bias_map, compute_horse_cta,
)
from build_pace_baseline import (
    build_baseline_table, compute_horse_pgr, compute_horse_pace_response,
)

DB_PATH = "keiba.db"
N_MC_DEFAULT = 500  # 動作確認用。本番2000回はn_mc引数で切替可能

# COURSE_ADV: run_mc_fixed(generate_mc_record.py:119-120)からそのまま継承
COURSE_ADV = {'東京': {2600: -8}, '中山': {3390: 5, 3110: 4},
              '阪神': {1800: 3}, '中京': {1800: 3, 2000: 3}}

# ── CTA項の係数 ─────────────────────────────────────────────
K_ABILITY = 1.0  # cta_z * K_ABILITY。初期値プレースホルダ(要calibration、Gate1以降)

# ── Grit項のスケール ─────────────────────────────────────────
# Grit_H/Grit_Sはrel_finish残差(±0.25キャップ、0-1スケール)なので、gainのpoint
# スケールに変換する係数。初期値プレースホルダ。
GRIT_SCALE = 20.0

# ── 風スロット係数(簡易回帰による概算値、要Gate1較正) ──────────
# W1: race_wind_v2 x race_laps.pace_type の実測(tail_home平均: H=-0.217/M=-0.023/S=+0.083)
#     から、tail_home 1単位あたりのH<->Sペース確率シフト量として概算。
WIND_A_PACE_SHIFT = 0.02
# W2: race_wind_v2 x results の簡易回帰(rel_finish ~ tail_home, 脚質別)から、
#     front(前/好位): slope=+0.00191(tailwindで不利) / closer(中団/後方): slope=-0.00202(有利)
#     をpointスケールに変換した係数。
WIND_B_STYLE = 0.15
# W3: 突風ノイズ拡大。noise_std = BASE_NOISE_STD + WIND_C_GUST * gust_max
BASE_NOISE_STD = 5.0
WIND_C_GUST = 0.3


def hash64_seed(race_id: str) -> int:
    """race_idから決定的な32bit seedを生成する(PYTHONHASHSEED依存のbuiltin hash()は
    使わない — 環境変数固定でも将来のPython版で挙動が変わりうるため、hashlibで固定)。"""
    h = hashlib.sha256(race_id.encode("utf-8")).hexdigest()
    return int(h[:16], 16) % (2 ** 32)


def _style_group(style):
    if style in ("逃げ", "先行"):
        return "front"
    return "closer"


def precompute_horse_features(conn, horses, race_info, class_par, k_cls, same_day_bias_map,
                               pace_baseline):
    """MC試行ループの外で1回だけ計算する馬ごとの静的特徴量
    (CTA, v_i(P), Grit_H, Grit_S)をhorsesの各dictに追加して返す。"""
    asof_date = race_info.get("date")
    distance = race_info.get("distance", 1600)
    surface = race_info.get("surface", "芝")

    for h in horses:
        name = h["horse_name"]
        cta = compute_horse_cta(conn, name, asof_date, class_par, k_cls, same_day_bias_map)
        h["cta_z"] = cta["cta_main"] if cta["cta_main"] is not None else 0.0

        pgr = compute_horse_pgr(conn, name, asof_date, pace_baseline)
        h["grit_h"] = pgr["grit_h"]
        h["grit_s"] = pgr["grit_s"]

        resp = compute_horse_pace_response(conn, name, asof_date, distance, surface,
                                            jockey=h.get("jockey", ""))
        h["pace_v"] = resp["v"]
        if "style" not in h or not h["style"]:
            h["style"] = resp["style"]
    return horses


def run_mc123(horses, race_info, n_mc=N_MC_DEFAULT, seed=None, wind=None):
    """MC本体。horsesは事前にprecompute_horse_features()済みのリストを渡すこと。

    Args:
        horses: list[dict] (horse_name, umaban, style, gate, cta_z, grit_h, grit_s, pace_v)
        race_info: dict (venue, distance, track_cond, num_horses, date, surface)
        n_mc: 試行回数
        seed: Noneならrace_idから決定的に生成
        wind: {'tail_home': float, 'gust_max': float} or None(中立シナリオ)

    Returns:
        list[dict]: horses と同じ順序で {'p1','p2','p3','ptop3'}
    """
    n = len(horses)
    if n < 3:
        return [{"p1": 1.0 / n, "p2": 1.0 / n, "p3": 1.0 / n, "ptop3": 1.0} for _ in range(n)]

    if seed is None:
        seed = hash64_seed(race_info.get("race_id", "unknown"))
    rng = np.random.default_rng(seed)

    tc = race_info.get("track_cond", "良")
    venue = race_info.get("venue", "")
    dist = race_info.get("distance", 1600)
    c_adj = COURSE_ADV.get(venue, {}).get(dist, 0)
    heavy = tc in ("重", "不良")
    hv = 3.0 if tc == "不良" else 2.0
    n_nige = sum(1 for h in horses if h["style"] == "逃げ")
    n_front = sum(1 for h in horses if h["style"] in ("逃げ", "先行"))
    num_h = race_info.get("num_horses", n)

    pH, pM, pS = 0.25, 0.40, 0.35
    if n_nige >= 3:
        adj = min(0.20, 0.08 * (n_nige - 1))
        pH = min(0.55, pH + adj); pS = max(0.10, pS - adj * 0.7); pM = max(0.20, pM - adj * 0.3)
    elif n_nige == 2:
        pH = min(0.50, pH + 0.08); pS = max(0.15, pS - 0.05)
    elif n_nige == 0:
        pH = max(0.05, pH - 0.10); pS = min(0.55, pS + 0.10)
    if n > 0 and n_front / n > 0.50:
        pH = min(0.50, pH + 0.05); pS = max(0.10, pS - 0.05)
    if num_h >= 17:
        pH = min(0.55, pH + 0.12); pS = max(0.10, pS - 0.09)
    elif num_h >= 13:
        pH = min(0.50, pH + 0.08); pS = max(0.10, pS - 0.06)
    elif num_h <= 8:
        pH = max(0.05, pH - 0.05); pS = min(0.55, pS + 0.04)
    inner = any(h.get("gate", 5) <= 4 and h["style"] in ("逃げ", "先行") for h in horses)
    outer = any(h.get("gate", 5) >= 5 and h["style"] in ("逃げ", "先行") for h in horses)
    if inner and outer:
        pH = min(0.55, pH + 0.03); pS = max(0.10, pS - 0.03)

    # ── W1: 風によるペース確率補正 ──
    if wind and wind.get("tail_home") is not None:
        th = wind["tail_home"]
        shift = WIND_A_PACE_SHIFT * th  # 正th(追風)→Sへシフト、負(向風)→Hへシフト
        pH = max(0.05, pH - shift)
        pS = max(0.05, pS + shift)

    t = pH + pM + pS
    pH /= t; pM /= t; pS /= t

    # ── W3: 突風によるノイズ拡大 ──
    noise_std = BASE_NOISE_STD
    if wind and wind.get("gust_max") is not None:
        noise_std = BASE_NOISE_STD + WIND_C_GUST * wind["gust_max"]

    tail_home = wind.get("tail_home", 0.0) if wind else 0.0

    rank_counts = np.zeros((3, n))  # [0]=1着, [1]=2着, [2]=3着

    for _ in range(n_mc):
        P = rng.choice(["H", "M", "S"], p=[pH, pM, pS])
        gain = np.zeros(n)
        for i, h in enumerate(horses):
            st = h["style"]
            # ── 個体化ペース応答 (STYLE_DEF静的値の代わりにv_i(P)を使用) ──
            v = h.get("pace_v", {}).get(P, 60)
            # ── CTA項 (last3f項の代替。上位互換のため削除) ──
            g = (75.0 * v / 100 - 70) * 0.45 + h.get("cta_z", 0.0) * K_ABILITY

            bon = 0.0
            if P == "S":
                if st == "逃げ":
                    bon = 2.0
                elif st == "先行":
                    bon = 0.5

            # ── シナリオ条件付きGrit: MC試行が引いたPに応じてのみ加算 ──
            if P == "H":
                bon += h.get("grit_h", 0.0) * GRIT_SCALE
            elif P == "S":
                bon += h.get("grit_s", 0.0) * GRIT_SCALE

            if heavy:
                if st == "逃げ":
                    bon += hv
                elif st == "先行":
                    bon += hv * 0.6
                elif st in ("差し", "追い込み"):
                    bon -= hv * 0.5
            if c_adj > 0 and st in ("逃げ", "先行"):
                bon += c_adj * 0.5
            elif c_adj < 0 and st in ("差し", "追い込み"):
                bon += abs(c_adj) * 0.5

            gate = h.get("gate", 4)
            if gate >= 6 and st in ("逃げ", "先行"):
                if any(hh.get("gate", 4) <= 4 and hh["style"] in ("逃げ", "先行")
                       for j2, hh in enumerate(horses) if j2 != i):
                    if P == "H":
                        g -= 1.0

            # ── W2: 脚質×直線風ボーナス ──
            if tail_home:
                if _style_group(st) == "front":
                    bon -= WIND_B_STYLE * tail_home  # 追風=front不利
                else:
                    bon += WIND_B_STYLE * tail_home  # 追風=closer有利

            gain[i] = g + bon

        noise = rng.normal(0, noise_std, n)
        order = np.argsort(-(gain + noise))
        for pos in range(min(3, n)):
            rank_counts[pos, order[pos]] += 1

    out = []
    for i in range(n):
        p1 = rank_counts[0, i] / n_mc
        p2 = rank_counts[1, i] / n_mc
        p3 = rank_counts[2, i] / n_mc
        out.append({"p1": round(p1, 4), "p2": round(p2, 4), "p3": round(p3, 4),
                     "ptop3": round(p1 + p2 + p3, 4)})
    return out


def _self_test():
    """this_week_races.jsonの実データ1レースで動作確認する。"""
    import json
    import time
    from pathlib import Path

    p = Path("this_week_races.json")
    if not p.exists():
        print("this_week_races.json が見つかりません。DBから直近レースで代替テストします。")
        return _self_test_from_db()

    races = json.load(open(p, encoding="utf-8"))
    race = next((r for r in races if r.get("horses") and len(r["horses"]) >= 5), None)
    if race is None:
        print("有効なレースが見つかりません。DBから代替テストします。")
        return _self_test_from_db()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = None
    print("=== 事前テーブル読み込み ===")
    t0 = time.time()
    class_par = build_class_par_table(conn, verbose=False)
    k_cls = calibrate_k_cls(conn, verbose=False)
    bias_map = load_same_day_bias_map(conn)
    pace_baseline = build_baseline_table(conn, verbose=False)
    print(f"  {time.time()-t0:.1f}秒")

    venue = race.get("venue", "")
    dist = race.get("distance") or 1600
    srf = race.get("surface", "芝")
    date = race.get("date")
    horses = []
    for hi, h in enumerate(race.get("horses", [])):
        hn = (h.get("name", "") or "").strip()
        if not hn:
            continue
        uma = h.get("umaban") or (hi + 1)
        horses.append({
            "horse_name": hn, "umaban": uma, "jockey": (h.get("jockey", "") or "").strip(),
            "gate": min(8, (uma - 1) // 2 + 1), "style": None,
        })

    race_info = {"venue": venue, "distance": dist, "track_cond": "良",
                 "num_horses": len(horses), "date": date, "surface": srf,
                 "race_id": f"{date}_{venue}_{race.get('race_num', 1)}"}

    print(f"\n=== テスト対象: {date} {venue} {race.get('race_num')}R {srf}{dist}m "
          f"({len(horses)}頭) ===")

    t0 = time.time()
    horses = precompute_horse_features(conn, horses, race_info, class_par, k_cls, bias_map,
                                        pace_baseline)
    t_feat = time.time() - t0

    # シナリオ1: 当日実況(簡易的に会場代表的な風値を仮定。実データJOINは今回省略)
    wind_actual = {"tail_home": 1.5, "gust_max": 8.0}
    t0 = time.time()
    result_actual = run_mc123(horses, race_info, n_mc=N_MC_DEFAULT, wind=wind_actual)
    t_mc_actual = time.time() - t0

    # シナリオ2: 中立(風なし・良馬場)
    t0 = time.time()
    result_neutral = run_mc123(horses, race_info, n_mc=N_MC_DEFAULT, wind=None)
    t_mc_neutral = time.time() - t0

    print(f"\n特徴量事前計算: {t_feat:.2f}秒 ({len(horses)}頭)")
    print(f"MC実行(当日実況シナリオ, n_mc={N_MC_DEFAULT}): {t_mc_actual:.2f}秒")
    print(f"MC実行(中立シナリオ, n_mc={N_MC_DEFAULT}): {t_mc_neutral:.2f}秒")

    print(f"\n{'馬名':<14}{'style':<6}{'CTA':>7}{'p1':>7}{'p2':>7}{'p3':>7}{'ptop3':>8}  [中立ptop3]")
    sum_p1 = 0.0
    for h, r_a, r_n in zip(horses, result_actual, result_neutral):
        sum_p1 += r_a["p1"]
        print(f"{h['horse_name']:<14}{h['style']:<6}{h['cta_z']:>7.2f}"
              f"{r_a['p1']:>7.3f}{r_a['p2']:>7.3f}{r_a['p3']:>7.3f}{r_a['ptop3']:>8.3f}"
              f"  [{r_n['ptop3']:.3f}]")

    print(f"\np1合計(当日実況シナリオ): {sum_p1:.4f} (1.0付近が妥当)")
    all_in_range = all(0 <= r["p1"] <= 1 and 0 <= r["p2"] <= 1 and 0 <= r["p3"] <= 1
                        for r in result_actual)
    print(f"全馬 p1/p2/p3 が[0,1]範囲内: {all_in_range}")

    conn.close()


def _self_test_from_db():
    """this_week_races.jsonが無い/使えない場合、DBの直近レースで代替テスト。"""
    import time
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT race_id, date, venue, race_num, surface, distance
        FROM race_laps ORDER BY date DESC LIMIT 1
    """).fetchone()
    race_id, date, venue, race_num, srf, dist = row
    runners = conn.execute("""
        SELECT horse_name, umaban, jockey FROM results
        WHERE race_id=? ORDER BY umaban
    """, (race_id,)).fetchall()
    horses = [{"horse_name": hn, "umaban": uma or (i + 1), "jockey": jk or "",
               "gate": min(8, ((uma or i + 1) - 1) // 2 + 1), "style": None}
              for i, (hn, uma, jk) in enumerate(runners) if hn]

    class_par = build_class_par_table(conn, verbose=False)
    k_cls = calibrate_k_cls(conn, verbose=False)
    bias_map = load_same_day_bias_map(conn)
    pace_baseline = build_baseline_table(conn, verbose=False)

    race_info = {"venue": venue, "distance": dist, "track_cond": "良",
                 "num_horses": len(horses), "date": date, "surface": srf, "race_id": race_id}
    print(f"=== DB代替テスト: {race_id} {srf}{dist}m ({len(horses)}頭) ===")
    horses = precompute_horse_features(conn, horses, race_info, class_par, k_cls, bias_map,
                                        pace_baseline)
    t0 = time.time()
    result = run_mc123(horses, race_info, n_mc=N_MC_DEFAULT, wind={"tail_home": 1.0, "gust_max": 5.0})
    t_mc = time.time() - t0
    print(f"MC実行時間: {t_mc:.2f}秒")
    sum_p1 = sum(r["p1"] for r in result)
    for h, r in zip(horses, result):
        print(f"  {h['horse_name']:<14}{h['style']:<6} p1={r['p1']:.3f} p2={r['p2']:.3f} "
              f"p3={r['p3']:.3f} ptop3={r['ptop3']:.3f}")
    print(f"p1合計: {sum_p1:.4f}")
    conn.close()


if __name__ == "__main__":
    _self_test()
