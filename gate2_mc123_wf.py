"""
Gate 2: F6複合フィルタ(単勝) + 真のWF CV(4fold, 2022-2025) + Winsorized ROI判定

F6(d) AND F2(g=0.05) AND F3(摂動安定性) AND veto(不良馬場/gust_max>=10/頭数<10)
事前登録グリッド: x_min in {0.25,0.30} x d in {0.03,0.05} の4セルのみ。
kill-switch: WF ROI(Winsorized)>=105% AND 年別ROI>85%が4年中3年以上 AND n>=120(4年合計)
のいずれかを満たすセルがあれば合格。2026年は一切使わない(holdout温存)。
"""
import sqlite3
import time
from collections import defaultdict

import mc123_engine
from mc123_engine import run_mc123, hash64_seed, N_MC_DEFAULT
from mc123_batch import (
    load_horse_hist_all, load_same_day_bias_dict, precompute_horse_features_fast,
)
from build_class_par import build_class_par_table, calibrate_k_cls
from build_pace_baseline import build_baseline_table

DB_PATH = "keiba.db"
FOLD_YEARS = [2022, 2023, 2024, 2025]
GRID = [(x, d) for x in (0.25, 0.30) for d in (0.03, 0.05)]
G_F2 = 0.05
WINSORIZE_CAP = 50000
STAKE = 100  # 単勝1点あたりの購入単位(円)


def umaban_to_gate(umaban):
    if not umaban or umaban <= 0:
        return 4
    return min((umaban + 1) // 2, 8)


def estimate_wind_ab(conn, cutoff_date):
    """quick_wind_coef.pyと同一ロジックをcutoff_date制限付きで再実行し、W1のa・W2のbを推定。"""
    rows = conn.execute("""
        SELECT rl.pace_type, rw.tail_home
        FROM race_wind_v2 rw JOIN race_laps rl ON rl.race_id = rw.race_id
        WHERE rl.pace_type IN ('H','M','S') AND rl.date < ?
    """, (cutoff_date,)).fetchall()
    by_pt = defaultdict(list)
    for pt, th in rows:
        by_pt[pt].append(th)
    avg_h = sum(by_pt["H"]) / len(by_pt["H"]) if by_pt["H"] else 0
    avg_s = sum(by_pt["S"]) / len(by_pt["S"]) if by_pt["S"] else 0
    # H<->S間のtail_home差をベースにa係数をスケール(全期間推定0.02との整合を取る簡易式)
    a = 0.02 * (avg_s - avg_h) / 0.30 if (avg_s - avg_h) != 0 else 0.02

    rows2 = conn.execute("""
        SELECT rw.tail_home, r.pos4, r.num_horses, r.finish
        FROM race_wind_v2 rw JOIN results r ON r.race_id = rw.race_id
        WHERE r.pos4 IS NOT NULL AND r.num_horses > 1 AND r.finish < 90 AND r.date < ?
    """, (cutoff_date,)).fetchall()
    front_x, front_y, closer_x, closer_y = [], [], [], []
    for th, pos4, nh, fin in rows2:
        ratio = pos4 / nh
        rel = (fin - 1) / (nh - 1)
        if ratio <= 0.45:
            front_x.append(th); front_y.append(rel)
        elif ratio > 0.70:
            closer_x.append(th); closer_y.append(rel)

    def slope(x, y):
        if len(x) < 50:
            return 0.002
        n = len(x); mx = sum(x) / n; my = sum(y) / n
        num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
        den = sum((xi - mx) ** 2 for xi in x)
        return num / den if den else 0.002

    b_front = abs(slope(front_x, front_y))
    b_closer = abs(slope(closer_x, closer_y))
    b = 0.15 * ((b_front + b_closer) / 2) / 0.002  # 全期間推定0.15との整合スケール
    return round(a, 4), round(b, 4)


def run_fold(conn, year, horse_hist, bias_map, n_mc=N_MC_DEFAULT, verbose=True):
    cutoff = f"{year}-01-01"
    class_par = build_class_par_table(conn, cutoff_date=cutoff, verbose=False)
    k_cls = calibrate_k_cls(conn, cutoff_date=cutoff, verbose=False)
    pace_baseline = build_baseline_table(conn, cutoff_date=cutoff, verbose=False)
    a, b = estimate_wind_ab(conn, cutoff)
    mc123_engine.WIND_A_PACE_SHIFT = a
    mc123_engine.WIND_B_STYLE = b
    if verbose:
        print(f"  [{year}] wind a={a} b={b} (fold内で先行年データのみから再推定)")

    races = conn.execute("""
        SELECT DISTINCT r.date, r.venue, r.race_num, r.race_id, r.surface, r.distance, r.track_cond
        FROM results r
        WHERE r.date >= ? AND r.date < ?
          AND r.surface IN ('芝','ダ')
          AND r.race_name NOT LIKE '%新馬%' AND r.race_name NOT LIKE '%未勝利%'
        ORDER BY r.date, r.venue, r.race_num
    """, (f"{year}-01-01", f"{year+1}-01-01")).fetchall()

    wind_map = {r[0]: {"tail_home": r[1], "gust_max": r[2]}
                for r in conn.execute("SELECT race_id, tail_home, gust_max FROM race_wind_v2")}
    dividend_map = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT race_id, tansho_umaban, tansho_payout FROM dividends")}

    bets = []
    t0 = time.time()
    for idx, (date, venue, race_num, race_id, srf, dist, tc) in enumerate(races):
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
                                        bias_map, pace_baseline)

        wind = wind_map.get(race_id)
        seed = hash64_seed(race_id)

        # veto
        if tc == "不良" or len(horses) < 10:
            continue
        gust = wind.get("gust_max") if wind else None
        if gust is not None and gust >= 10:
            continue

        try:
            r_actual = run_mc123(horses, race_info, n_mc=n_mc, seed=seed, wind=wind)
            r_neutral = run_mc123(horses, race_info, n_mc=n_mc, seed=seed, wind=None)
        except Exception:
            continue

        # F3摂動: tail_homeを+-20%
        wind_up = wind_down = None
        if wind and wind.get("tail_home") is not None:
            wind_up = {"tail_home": wind["tail_home"] * 1.2, "gust_max": wind.get("gust_max")}
            wind_down = {"tail_home": wind["tail_home"] * 0.8, "gust_max": wind.get("gust_max")}
        try:
            r_up = run_mc123(horses, race_info, n_mc=n_mc, seed=seed, wind=wind_up) if wind_up else r_actual
            r_down = run_mc123(horses, race_info, n_mc=n_mc, seed=seed, wind=wind_down) if wind_down else r_actual
        except Exception:
            r_up, r_down = r_actual, r_actual

        p1_actual = [r["p1"] for r in r_actual]
        p1_neutral = [r["p1"] for r in r_neutral]
        top_idx = max(range(len(horses)), key=lambda i: p1_actual[i])
        sorted_p1 = sorted(p1_actual, reverse=True)
        gap = sorted_p1[0] - (sorted_p1[1] if len(sorted_p1) > 1 else 0.0)
        top_idx_up = max(range(len(horses)), key=lambda i: r_up[i]["p1"])
        top_idx_down = max(range(len(horses)), key=lambda i: r_down[i]["p1"])
        f3_ok = (top_idx_up == top_idx) and (top_idx_down == top_idx)
        f2_ok = gap >= G_F2

        div_uma, div_payout = dividend_map.get(race_id, (None, None))
        h = horses[top_idx]
        diff_p1 = p1_actual[top_idx] - p1_neutral[top_idx]

        bets.append({
            "date": date, "year": year, "race_id": race_id, "horse_name": h["horse_name"],
            "umaban": h["umaban"], "p1_actual": p1_actual[top_idx], "p1_neutral": p1_neutral[top_idx],
            "diff_p1": diff_p1, "gap": gap, "f2_ok": f2_ok, "f3_ok": f3_ok,
            "won": div_uma is not None and h["umaban"] == div_uma,
            "payout": div_payout if (div_uma is not None and h["umaban"] == div_uma) else 0,
        })

        if verbose and idx % 1500 == 0 and idx > 0:
            print(f"    {idx}/{len(races)}R  {time.time()-t0:.1f}s")

    if verbose:
        print(f"  [{year}] {len(races)}R対象 -> {len(bets)}R veto後  {time.time()-t0:.1f}s")
    return bets


def evaluate_grid(all_bets):
    results = {}
    for x_min, d in GRID:
        cell_bets_by_year = defaultdict(list)
        for b in all_bets:
            if b["f2_ok"] and b["f3_ok"] and b["p1_actual"] >= x_min and b["diff_p1"] >= d:
                cell_bets_by_year[b["year"]].append(b)

        all_cell_bets = [b for yr in cell_bets_by_year.values() for b in yr]
        n = len(all_cell_bets)
        inv = n * STAKE
        ret_win = sum(min(b["payout"], WINSORIZE_CAP) for b in all_cell_bets)
        roi = ret_win / inv * 100 if inv else 0.0

        year_roi = {}
        for yr in FOLD_YEARS:
            yb = cell_bets_by_year.get(yr, [])
            if yb:
                yi = len(yb) * STAKE
                yr_ret = sum(min(b["payout"], WINSORIZE_CAP) for b in yb)
                year_roi[yr] = round(yr_ret / yi * 100, 1)
            else:
                year_roi[yr] = None

        n_years_gt85 = sum(1 for yr in FOLD_YEARS if year_roi[yr] is not None and year_roi[yr] > 85)
        passed = (roi >= 105.0) and (n_years_gt85 >= 3) and (n >= 120)

        results[(x_min, d)] = {
            "n": n, "roi_winsorized": round(roi, 1), "year_roi": year_roi,
            "n_years_gt85": n_years_gt85, "passed": passed,
        }
    return results


def main():
    t_total = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    print("=== 一括ロード ===")
    horse_hist = load_horse_hist_all(conn)
    bias_map = load_same_day_bias_dict(conn)

    all_bets = []
    for year in FOLD_YEARS:
        print(f"\n=== fold {year} ===")
        bets = run_fold(conn, year, horse_hist, bias_map)
        all_bets.extend(bets)

    conn.close()
    print(f"\n総所要時間: {time.time()-t_total:.1f}s  総候補数(veto後): {len(all_bets)}")

    print("\n=== グリッド評価(4セル) ===")
    results = evaluate_grid(all_bets)
    any_pass = False
    for (x_min, d), r in results.items():
        print(f"x_min={x_min} d={d}: n={r['n']} WF-ROI(Winsorized)={r['roi_winsorized']}% "
              f"年別={r['year_roi']} 85%超年数={r['n_years_gt85']}/4 "
              f"-> {'合格' if r['passed'] else '不合格'}")
        any_pass = any_pass or r["passed"]

    print(f"\n=== kill-switch最終判定: {'合格(採用可)' if any_pass else '不合格(不採用)'} ===")
    return results


if __name__ == "__main__":
    main()
