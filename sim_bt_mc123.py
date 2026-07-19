"""
sim_bt_mc123.py — Gate 1: mc123_engine.py のleak-free全馬BT + 較正チェック

sim_bt_full.pyのBTハーネス構造(全期間一括ロード→レースループ)を踏襲。
class_par/k_cls/pace_baselineは年ごとのcutoff_date(その年の1/1より前のデータのみ)で
再構築し、リークを防止する。race_wind_v2は「当日の風」であり未来情報ではないため
cutoff不要(全期間分をそのまま使用)。

Gate1は「較正の良し悪し」のみを見る(ROIは何も保証しない。Gate2で別途検証)。
"""
import sqlite3
import json
import time
from collections import defaultdict

from build_class_par import build_class_par_table, calibrate_k_cls
from build_pace_baseline import build_baseline_table
from mc123_batch import (
    load_horse_hist_all, load_same_day_bias_dict, precompute_horse_features_fast,
)
from mc123_engine import run_mc123, hash64_seed, N_MC_DEFAULT
from backtest_sim_lite import run_mc_lite, STYLE_DEF
import generate_race_sim as gsim

DB_PATH = "keiba.db"
OUTPUT_PATH = "mc123_bt_results.json"


def umaban_to_gate(umaban):
    if not umaban or umaban <= 0:
        return 4
    return min((umaban + 1) // 2, 8)


def run_bt_for_year(conn, year, horse_hist, bias_map, n_mc=N_MC_DEFAULT, verbose=True):
    cutoff = f"{year}-01-01"
    if verbose:
        print(f"  [{year}] class_par/k_cls/pace_baseline 構築(cutoff={cutoff})...", end="", flush=True)
    t0 = time.time()
    class_par = build_class_par_table(conn, cutoff_date=cutoff, verbose=False)
    k_cls = calibrate_k_cls(conn, cutoff_date=cutoff, verbose=False)
    pace_baseline = build_baseline_table(conn, cutoff_date=cutoff, verbose=False)
    if verbose:
        print(f" {time.time()-t0:.1f}s")

    races = conn.execute("""
        SELECT DISTINCT r.date, r.venue, r.race_num, r.race_id, r.surface, r.distance, r.track_cond
        FROM results r
        WHERE r.date >= ? AND r.date < ?
          AND r.surface IN ('芝','ダ')
          AND r.race_name NOT LIKE '%新馬%' AND r.race_name NOT LIKE '%未勝利%'
        ORDER BY r.date, r.venue, r.race_num
    """, (f"{year}-01-01", f"{year+1}-01-01")).fetchall()

    wind_map = {}
    for race_id, tail_home, gust_max in conn.execute(
        "SELECT race_id, tail_home, gust_max FROM race_wind_v2"
    ):
        wind_map[race_id] = {"tail_home": tail_home, "gust_max": gust_max}

    records = []
    t0 = time.time()
    n_skipped = 0
    for idx, (date, venue, race_num, race_id, srf, dist, tc) in enumerate(races):
        runners = conn.execute(
            "SELECT horse_name, umaban, jockey, finish, num_horses, horse_num FROM results WHERE race_id=?",
            (race_id,)
        ).fetchall()
        if len(runners) < 4:
            n_skipped += 1
            continue

        horses = []
        for hn, uma, jk, fin, nh, hnum in runners:
            hn = (hn or "").strip()
            if not hn:
                continue
            # 公平なAYO比較のため前走last3fを取得(sim_bt_full.py run_bt()と同一ロジック)。
            # last3f=None固定だとrun_mc_liteの脚質×last3f項が全馬同値になり不当に弱体化するため。
            prev_hist = [e for e in horse_hist.get(hn, []) if e["date"] < date]
            prev_l3f = next((e["last3f"] for e in reversed(prev_hist) if e.get("last3f")), None)
            horses.append({
                "horse_name": hn, "umaban": uma or hnum, "jockey": (jk or "").strip(),
                "gate": umaban_to_gate(uma if uma is not None else hnum),
                "style": None, "finish": fin, "prev_last3f": prev_l3f,
            })
        if len(horses) < 4:
            n_skipped += 1
            continue

        race_info = {
            "venue": venue, "distance": dist or 1600, "track_cond": tc or "良",
            "num_horses": len(horses), "date": date, "surface": srf, "race_id": race_id,
        }
        precompute_horse_features_fast(horses, race_info, horse_hist, class_par, k_cls,
                                        bias_map, pace_baseline)

        wind = wind_map.get(race_id)
        seed = hash64_seed(race_id)
        try:
            mc_result = run_mc123(horses, race_info, n_mc=n_mc, seed=seed, wind=wind)
        except Exception:
            n_skipped += 1
            continue

        # 比較用: 既存AYO系(backtest_sim_lite.run_mc_lite)のtop3率も同時算出
        lite_horses = [{"horse_name": h["horse_name"], "style": h["style"],
                         "last3f": h.get("prev_last3f"), "gate": h["gate"]} for h in horses]
        try:
            lite_top3 = run_mc_lite(lite_horses, race_info, n_mc=n_mc)
        except Exception:
            lite_top3 = [None] * len(horses)

        for h, mc, lt3 in zip(horses, mc_result, lite_top3):
            records.append({
                "date": date, "venue": venue, "race_num": race_num, "race_id": race_id,
                "horse_name": h["horse_name"], "style": h["style"],
                "p1": mc["p1"], "p2": mc["p2"], "p3": mc["p3"], "ptop3": mc["ptop3"],
                "ayo_top3_rate": round(float(lt3), 3) if lt3 is not None else None,
                "actual_finish": h["finish"],
                "win": h["finish"] is not None and float(h["finish"]) == 1.0,
                "hit_top3": h["finish"] is not None and float(h["finish"]) <= 3.0,
            })

        if verbose and idx % 1000 == 0 and idx > 0:
            elapsed = time.time() - t0
            eta = elapsed / idx * (len(races) - idx)
            print(f"    {idx}/{len(races)}R  {elapsed:.1f}s経過  残り{eta:.1f}s")

    elapsed = time.time() - t0
    if verbose:
        print(f"  [{year}] MC計算完了: {len(races)}R中{len(races)-n_skipped}R処理  {elapsed:.1f}s "
              f"({elapsed/max(1,len(races)-n_skipped)*1000:.1f}ms/R)")
    return records


def calibration_check(records):
    """予測p1が10%帯(9-11%)の馬群の実際勝率を確認"""
    band = [r for r in records if 0.09 <= r["p1"] <= 0.11]
    if not band:
        return None
    actual_win_rate = sum(1 for r in band if r["win"]) / len(band) * 100
    return {"n": len(band), "predicted_band": "9-11%", "actual_win_rate_pct": round(actual_win_rate, 2)}


def ayo_comparison(records):
    """MC-P1最高馬の実際勝率 vs AYO(top3率)最高馬の実際勝率"""
    by_race = defaultdict(list)
    for r in records:
        by_race[r["race_id"]].append(r)

    mc123_top_wins, mc123_top_n = 0, 0
    ayo_top_wins, ayo_top_n = 0, 0
    for race_id, rs in by_race.items():
        best_mc = max(rs, key=lambda r: r["p1"])
        mc123_top_n += 1
        if best_mc["win"]:
            mc123_top_wins += 1

        ayo_candidates = [r for r in rs if r["ayo_top3_rate"] is not None]
        if ayo_candidates:
            best_ayo = max(ayo_candidates, key=lambda r: r["ayo_top3_rate"])
            ayo_top_n += 1
            if best_ayo["win"]:
                ayo_top_wins += 1

    mc123_rate = mc123_top_wins / mc123_top_n * 100 if mc123_top_n else 0
    ayo_rate = ayo_top_wins / ayo_top_n * 100 if ayo_top_n else 0
    return {
        "mc123_top_p1_win_rate_pct": round(mc123_rate, 2), "mc123_n_races": mc123_top_n,
        "ayo_top_rate_win_rate_pct": round(ayo_rate, 2), "ayo_n_races": ayo_top_n,
        "diff_pt": round(mc123_rate - ayo_rate, 2),
    }


def main(years, n_mc=N_MC_DEFAULT):
    t_total = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    print("=== 1回きりの一括ロード ===")
    t0 = time.time()
    horse_hist = load_horse_hist_all(conn)
    bias_map = load_same_day_bias_dict(conn)
    print(f"horse_hist: {time.time()-t0:.1f}s, {len(horse_hist):,}頭")

    all_records = []
    for year in years:
        print(f"\n=== {year}年 ===")
        recs = run_bt_for_year(conn, year, horse_hist, bias_map, n_mc=n_mc)
        all_records.extend(recs)

    conn.close()

    print(f"\n総所要時間: {time.time()-t_total:.1f}s  総レコード数: {len(all_records):,}")

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"years": years, "n_mc": n_mc, "records": all_records}, f,
                   ensure_ascii=False, default=str)
    print(f"-> {OUTPUT_PATH} 保存完了")

    print("\n=== Gate1 判定基準1: 較正曲線(p1=9-11%帯) ===")
    cal = calibration_check(all_records)
    print(cal)

    print("\n=== Gate1 判定基準2: AYO版との比較 ===")
    comp = ayo_comparison(all_records)
    print(comp)

    return all_records, cal, comp


if __name__ == "__main__":
    import sys
    years = [int(y) for y in sys.argv[1:]] if len(sys.argv) > 1 else [2021, 2022, 2023, 2024, 2025]
    main(years)
