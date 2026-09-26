# -*- coding: utf-8 -*-
"""
analyze_mc123_top1_conditions.py — MC123の1位予想馬が実際に複勝圏内(3着以内)に入りやすい
条件を実データで探す分析(2026-08-09、のりお依頼)。

「混戦度」というモデル内在指標ではなく、「会場×距離」「mc_dyn展開予想のH/M/S率が極端な時」
という実測条件でMC123 #1ピックの複勝的中率がどう変わるかを調べる。AYOkeibaの本番スコープと
同一基準(芝・ダート全レース、新馬・障害のみ除外、未勝利は含む)でtier_scope.pyのTIER_START_DATE
以降(2026-09-26改訂で2021-01-01、京都は改修後の2023-04-22以降のみ)を対象とする。

方法(リーク防止・効率化): calibrate_mc123.load_year_racesと同じ設計で年ごとに
cutoff_date=年始で構造テーブル(class_par/k_cls/pace_baseline/rank_par/margin_par/l3f_par/
horse_hist/bias_map)を構築するが、レース単位ではなく**年単位でPoolを作り直し、
各ワーカープロセスがinitializerで年1回だけテーブルを構築してグローバルにキャッシュする**
(毎レース再構築だと7000件規模で非現実的な時間になるため)。
MC123はn_mc=1000(バックテスト用軽量化)、mc_dyn展開予想は既存のcmd_validate/
compute_formation_accuracy.pyと同じn_sim=80を使用。

使い方: py -3 analyze_mc123_top1_conditions.py [--workers 14] [--cap N]
"""
import sys
import json
import time
import random
import sqlite3
import argparse
from pathlib import Path
from collections import defaultdict
from multiprocessing import Pool

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tier_scope import TIER_START_DATE, TIER_VENUE_START, in_tier_scope, scope_label

DB_PATH = str(Path(__file__).resolve().parent / "keiba.db")
N_MC = 1000
N_SIM_PACE = 80

_CTX = {}  # ワーカープロセスごとのキャッシュ(initializerで1回だけ設定)


def umaban_to_gate(umaban):
    if not umaban or umaban <= 0:
        return 4
    return min((umaban + 1) // 2, 8)


# 障害レース専用の距離集合は mc_dyn_engine.JUMP_DISTANCES に集約済み(2026-09-20、F2修正で
# generate_mc123_forecast.py等の本番3ファイルと共通化。このファイルで発見・確定した集合を
# is_jump_race()の一次データとして移設した)。


def fetch_target_races(conn):
    from mc_dyn_engine import pace_cls_group, is_jump_race
    rows = conn.execute("""
        SELECT DISTINCT race_id, venue, surface, distance, race_name, date
        FROM results
        WHERE date >= ? AND surface IN ('芝','ダ') AND num_horses >= 6 AND pos4 IS NOT NULL
          AND track_cond IS NOT NULL AND track_cond != ''
    """, (TIER_START_DATE,)).fetchall()
    out = []
    n_scope_excluded = 0
    for race_id, venue, surface, distance, rname, race_date in rows:
        if pace_cls_group(rname) == "新馬":
            continue
        if is_jump_race(rname, surface, distance):
            continue
        if not in_tier_scope(venue, race_date):
            n_scope_excluded += 1
            continue
        out.append((race_id, venue, surface, distance, rname, race_date))
    print(f"  会場別開始日で除外: {n_scope_excluded}件")
    return out


def _init_worker(cutoff):
    """年ごとにPoolを作り直す際、各ワーカープロセスの起動時に1回だけ呼ばれる。
    構造テーブルをこのプロセスのグローバル(_CTX)に構築してキャッシュする。"""
    import os as _os
    global _keep_alive_ref
    _keep_alive_ref = sys.stdout = open(_os.devnull, "w", encoding="utf-8")
    import sqlite3 as _sq
    from mc123_batch import load_horse_hist_all, load_same_day_bias_dict
    from build_class_par import build_class_par_table, calibrate_k_cls
    from build_pace_baseline import build_baseline_table
    from build_extra_par import build_rank_par, build_margin_par, build_l3f_par

    conn = _sq.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    _CTX["conn"] = conn
    _CTX["horse_hist"] = load_horse_hist_all(conn)
    _CTX["bias_map"] = load_same_day_bias_dict(conn)
    _CTX["class_par"] = build_class_par_table(conn, cutoff_date=cutoff, verbose=False)
    _CTX["k_cls"] = calibrate_k_cls(conn, cutoff_date=cutoff, verbose=False)
    _CTX["pace_baseline"] = build_baseline_table(conn, cutoff_date=cutoff, verbose=False)
    _CTX["rank_par"] = build_rank_par(conn, cutoff_date=cutoff, verbose=False)
    _CTX["margin_par"] = build_margin_par(conn, cutoff_date=cutoff, verbose=False)
    _CTX["l3f_par"] = build_l3f_par(conn, cutoff_date=cutoff, verbose=False)


def _worker(args):
    race_id, venue, surface, distance, rname, race_date = args
    from predict_race_formation import predict_formation
    from mc123_batch import precompute_horse_features_fast
    from mc123_engine import run_mc123, hash64_seed
    import generate_pace_forecast as gpf

    conn = _CTX["conn"]
    try:
        horses_pf, _est, _n_scr = gpf.fetch_horses(conn, race_id)
        if len(horses_pf) < 6:
            return None
        race = {"date": race_date, "venue": venue, "surface": surface, "distance": distance,
                "num_horses": len(horses_pf), "track_cond": "良", "race_name": rname}
        out = predict_formation(conn, race, horses_pf, n_sim=N_SIM_PACE, seed=1)
        if out is None or out.get("excluded"):
            return None
        p = out["pace"]
        if p["h_rate"] is None:
            return None

        horses_mc = []
        for i, h in enumerate(horses_pf):
            uma = h["umaban"]
            horses_mc.append({"horse_name": h["horse_name"], "umaban": uma,
                              "jockey": h["jockey"] or "", "gate": umaban_to_gate(uma),
                              "style": out["c2"][i]["style"], "finish": h["finish"]})

        race_info_mc = {"venue": venue, "distance": distance, "track_cond": "良",
                        "num_horses": len(horses_mc), "date": race_date, "surface": surface,
                        "race_id": race_id}
        precompute_horse_features_fast(horses_mc, race_info_mc, _CTX["horse_hist"], _CTX["class_par"],
                                        _CTX["k_cls"], _CTX["bias_map"], _CTX["pace_baseline"],
                                        rank_par=_CTX["rank_par"], margin_par=_CTX["margin_par"],
                                        l3f_par=_CTX["l3f_par"])
        seed = hash64_seed(race_id)
        result = run_mc123(horses_mc, race_info_mc, n_mc=N_MC, seed=seed, wind=None)
        top1_idx = max(range(len(horses_mc)), key=lambda i: result[i]["ptop3"])
        fin = horses_mc[top1_idx]["finish"]
        top1_placed = 1 if (fin and 1 <= fin <= 3) else 0

        return (venue, surface, distance, top1_placed, p["h_rate"], p["m_rate"], p["s_rate"], race_date)
    except Exception as e:
        return ("ERR", str(e), race_id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=14)
    ap.add_argument("--cap", type=int, default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    targets = fetch_target_races(conn)
    conn.close()
    random.seed(20260809)
    random.shuffle(targets)
    if args.cap:
        targets = targets[:args.cap]
    print(f"対象レース数: {len(targets)}")

    by_year = defaultdict(list)
    for t in targets:
        by_year[t[5][:4]].append(t)
    print("年別内訳:", {y: len(v) for y, v in sorted(by_year.items())})

    t0 = time.time()
    results, errors = [], []
    for year in sorted(by_year):
        cutoff = f"{year}-01-01"
        year_targets = by_year[year]
        print(f"=== {year}年({len(year_targets)}件) cutoff={cutoff} でPool構築中... ===")
        ty0 = time.time()
        with Pool(args.workers, initializer=_init_worker, initargs=(cutoff,)) as pool:
            for i, r in enumerate(pool.imap_unordered(_worker, year_targets, chunksize=4)):
                if r is None:
                    continue
                if r[0] == "ERR":
                    errors.append(r)
                else:
                    results.append(r)
                if (i + 1) % 500 == 0:
                    print(f"  {year}年 {i+1}/{len(year_targets)} 完了 ({time.time()-ty0:.0f}秒)")
        print(f"  {year}年完了({time.time()-ty0:.0f}秒)")
    elapsed = time.time() - t0
    print(f"完了: {len(results)}件成功 / {len(errors)}件エラー / 総{elapsed:.0f}秒")
    if errors:
        print("エラー例:", errors[:5])

    # 期間別診断(2026-09-26新設): 選出ゲートには使わない参考情報として、会場×距離×surface
    # セルごとにplace_rateを2021-2023/2024-の2期間で分けて記録し、非定常性を目視確認できるようにする
    PERIOD_CUTOFF = "2024-01-01"

    by_vd = defaultdict(list)
    by_vd_period = defaultdict(lambda: defaultdict(list))
    for venue, surface, distance, placed, h, m, s, race_date in results:
        by_vd[(venue, surface, distance)].append(placed)
        period = "2021-2023" if race_date < PERIOD_CUTOFF else "2024-"
        by_vd_period[(venue, surface, distance)][period].append(placed)

    buckets = {
        "S>=0.6": lambda h, m, s: s >= 0.6,
        "S 0.4-0.6": lambda h, m, s: 0.4 <= s < 0.6,
        "H>=0.6": lambda h, m, s: h >= 0.6,
        "H 0.4-0.6": lambda h, m, s: 0.4 <= h < 0.6,
        "M>=0.6": lambda h, m, s: m >= 0.6,
        "その他(混合)": lambda h, m, s: not (s >= 0.4 or h >= 0.4 or m >= 0.6),
    }
    bucket_stats = {}
    for label, cond in buckets.items():
        placed_list = [placed for venue, surface, distance, placed, h, m, s, race_date in results if cond(h, m, s)]
        if placed_list:
            bucket_stats[label] = {"n": len(placed_list),
                                   "place_rate": round(sum(placed_list) / len(placed_list), 4)}

    overall = sum(r[3] for r in results) / len(results) if results else None

    vd_stats = []
    for (venue, surface, distance), placed_list in by_vd.items():
        period_stats = {}
        for period, plist in by_vd_period[(venue, surface, distance)].items():
            if plist:
                period_stats[period] = {"n": len(plist), "place_rate": round(sum(plist) / len(plist), 4)}
        vd_stats.append({"venue": venue, "surface": surface, "distance": distance,
                         "n": len(placed_list), "place_rate": round(sum(placed_list) / len(placed_list), 4),
                         "period_stats": period_stats})
    vd_stats.sort(key=lambda x: -x["place_rate"])

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "n_races": len(results), "n_mc": N_MC, "n_sim_pace": N_SIM_PACE,
        "overall_place_rate": round(overall, 4) if overall is not None else None,
        "scope": scope_label(), "start_date": TIER_START_DATE, "venue_start_overrides": TIER_VENUE_START,
        "pace_bucket_stats": bucket_stats,
        "venue_distance_stats": vd_stats,
    }
    out_path = Path(__file__).resolve().parent / "mc123_top1_conditions.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"書き出し: {out_path}")
    print(f"全体複勝的中率: {overall:.4f}" if overall is not None else "N/A")
    print("ペース率バケット別:")
    for label, st in bucket_stats.items():
        print(f"  {label}: n={st['n']} 複勝的中率={st['place_rate']}")
    print("会場×距離 上位10(n>=15):")
    for st in [x for x in vd_stats if x["n"] >= 15][:10]:
        print(f"  {st['venue']}{st['surface']}{st['distance']}m: n={st['n']} 複勝的中率={st['place_rate']}")


if __name__ == "__main__":
    main()
