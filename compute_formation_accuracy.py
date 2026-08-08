# -*- coding: utf-8 -*-
"""
会場×表面×距離別の隊列予測精度(ρ)を計算し formation_accuracy.json を生成する(2026-08-08新規)。

方法(委員会承認済み設計、事前登録):
  1. 2024-01-01以降・新馬戦除く・6頭立て以上のレースをpredict_formation(n_sim=80)で検証
     (n_sim=80は既存cmd_validateの実測済み設定を踏襲。ヘッドラインは4角(pos4)基準、
     ゴール(finish)基準は弱さの併記用に別途算出)
  2. 会場×表面×距離セルごとに集計。within-race分散はセル内の残差から実測プール推定
  3. Cochran's Q による異質性検定(有意水準0.05は計算前に固定、事後のp-hackingを避ける)
  4. 有意なら DerSimonian-Laird 式で経験ベイズ縮小推定 → 信頼できるセル(n>=15)の
     分布から高/中/低の3分位境界を決定
  5. 有意でなければ会場×距離表示は不採用(heterogeneous=falseを出力し、フロント側は
     別途レース個別信頼度指標の検証を待つ)

使い方: py -3 compute_formation_accuracy.py [--n-races-cap N] [--workers N]
"""
import sys
import io
import json
import time
import random
import sqlite3
import argparse
import hashlib
from pathlib import Path
from collections import defaultdict
from multiprocessing import Pool

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DB_PATH = str(Path(__file__).resolve().parent / "keiba.db")
START_DATE = "2024-01-01"
MIN_CELL_N_RELIABLE = 15   # 異質性検定・分位境界決定に使う最小セル件数
ALPHA = 0.05                # 事前固定の有意水準(結果を見てからの変更は禁止)
N_SIM_VALIDATE = 80


def fetch_target_races(conn):
    from mc_dyn_engine import pace_cls_group
    # track_condが欠損の行(約7%)は実際の馬場状態を推測できないため除外する
    # (「良」に固定してシミュレーションすると馬場補正が体系的に外れ、精度計算が歪む)
    rows = conn.execute("""
        SELECT DISTINCT race_id, venue, surface, distance, race_name, track_cond, date
        FROM results
        WHERE date >= ? AND surface IN ('芝','ダ') AND num_horses >= 6 AND pos4 IS NOT NULL
          AND track_cond IS NOT NULL AND track_cond != ''
    """, (START_DATE,)).fetchall()
    out = []
    for race_id, venue, surface, distance, rname, track_cond, race_date in rows:
        if pace_cls_group(rname) == "新馬":
            continue
        out.append((race_id, venue, surface, distance, rname, track_cond, race_date))
    return out


def _worker(args):
    race_id, venue, surface, distance, race_name, track_cond, race_date = args
    # Windows spawnワーカーではpredict_race_formation.py側が
    # `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)` を実行する際、参照が切れた
    # 旧sys.stdoutが即GCされ、共有中のbufferごと閉じてしまい"I/O operation on closed file"に
    # なる(CPythonの参照カウントGCの落とし穴)。旧オブジェクトを_keep_aliveで保持して延命させる
    import os as _os
    _keep_alive = sys.stdout = open(_os.devnull, "w", encoding="utf-8")
    globals()["_keep_alive_ref"] = _keep_alive  # モジュールレベルにも保持し関数終了後もGCされないようにする
    import sqlite3 as _sq
    from scipy.stats import spearmanr
    from predict_race_formation import predict_formation
    import generate_pace_forecast as gpf
    conn = _sq.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = _sq.Row
    try:
        horses, _est = gpf.fetch_horses(conn, race_id)
        if len(horses) < 6:
            return None
        race = {"date": race_date, "venue": venue, "surface": surface,
                "distance": distance, "num_horses": len(horses),
                "track_cond": track_cond, "race_name": race_name}
        out = predict_formation(conn, race, horses, n_sim=N_SIM_VALIDATE, seed=1)
        if out is None or out.get("excluded"):
            return None
        mr = out["mean_ranks"]
        n_zones_key = [k for k in mr if k.endswith("_out") and k.startswith("zone")]
        key4 = sorted(n_zones_key)[-1] if n_zones_key else None
        rho4 = rho_goal = None
        if key4 and key4 in mr:
            pairs = [(mr[key4][i], h["pos4"]) for i, h in enumerate(horses) if h["pos4"] and h["pos4"] > 0]
            if len(pairs) >= 6:
                r = spearmanr([p[0] for p in pairs], [p[1] for p in pairs]).statistic
                if r == r:
                    rho4 = r
        if "goal" in mr:
            pairs = [(mr["goal"][i], h["finish"]) for i, h in enumerate(horses) if h["finish"] and h["finish"] > 0]
            if len(pairs) >= 6:
                r = spearmanr([p[0] for p in pairs], [p[1] for p in pairs]).statistic
                if r == r:
                    rho_goal = r
        if rho4 is None and rho_goal is None:
            return None
        return (venue, surface, distance, rho4, rho_goal)
    except Exception as e:
        return ("ERR", str(e), race_id, None, None)
    finally:
        conn.close()


def heterogeneity_test(cell_means, cell_ns, sigma2_pool):
    """Cochran's Q(固定効果版)。n>=MIN_CELL_N_RELIABLEのセルのみ対象。戻り値: (Q, df, p, weights)"""
    from scipy.stats import chi2
    items = [(m, n) for m, n in zip(cell_means, cell_ns) if n >= MIN_CELL_N_RELIABLE]
    if len(items) < 2:
        return None
    weights = [n / sigma2_pool for _, n in items]
    grand = sum(w * m for (m, _), w in zip(items, weights)) / sum(weights)
    Q = sum(w * (m - grand) ** 2 for (m, _), w in zip(items, weights))
    df = len(items) - 1
    p = 1 - chi2.cdf(Q, df) if df > 0 else 1.0
    return {"Q": Q, "df": df, "p": p, "grand_mean": grand, "n_cells_tested": len(items)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-races-cap", type=int, default=None, help="デバッグ用: 総レース数を上限で絞る")
    ap.add_argument("--workers", type=int, default=14)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    targets = fetch_target_races(conn)
    conn.close()
    random.seed(20260808)
    random.shuffle(targets)
    if args.n_races_cap:
        targets = targets[:args.n_races_cap]
    print(f"対象レース数: {len(targets)}")

    t0 = time.time()
    results = []
    with Pool(args.workers) as pool:
        for i, r in enumerate(pool.imap_unordered(_worker, targets, chunksize=8)):
            if r is not None:
                results.append(r)
            if (i + 1) % 500 == 0:
                elapsed = time.time() - t0
                print(f"  {i+1}/{len(targets)} 完了 ({elapsed:.0f}秒経過)")
    elapsed = time.time() - t0
    errors = [r for r in results if r[0] == "ERR"]
    results = [r for r in results if r[0] != "ERR"]
    print(f"完了: {len(results)}件成功 / {len(errors)}件エラー / {elapsed:.0f}秒")
    if errors:
        print("エラー例:", errors[:3])

    # セル集計
    cell_rho4 = defaultdict(list)
    cell_rho_goal = defaultdict(list)
    for venue, surface, distance, rho4, rho_goal in results:
        key = (venue, surface, distance)
        if rho4 is not None:
            cell_rho4[key].append(rho4)
        if rho_goal is not None:
            cell_rho_goal[key].append(rho_goal)

    all_rho4 = [r for vals in cell_rho4.values() for r in vals]
    grand_mean_rho4 = sum(all_rho4) / len(all_rho4) if all_rho4 else None
    all_rho_goal = [r for vals in cell_rho_goal.values() for r in vals]
    grand_mean_rho_goal = sum(all_rho_goal) / len(all_rho_goal) if all_rho_goal else None

    # within-cell分散のプール推定(n>=2のセルのみ)
    ss, dof = 0.0, 0
    for vals in cell_rho4.values():
        if len(vals) >= 2:
            m = sum(vals) / len(vals)
            ss += sum((v - m) ** 2 for v in vals)
            dof += len(vals) - 1
    sigma2_pool = ss / dof if dof > 0 else 0.04  # フォールバック: 実測既知値0.20^2

    cell_means = {k: sum(v) / len(v) for k, v in cell_rho4.items()}
    cell_ns = {k: len(v) for k, v in cell_rho4.items()}
    het = heterogeneity_test(list(cell_means.values()), list(cell_ns.values()), sigma2_pool)

    cells_out = []
    heterogeneous = bool(het and het["p"] < ALPHA)
    if heterogeneous:
        Q, df, grand = het["Q"], het["df"], het["grand_mean"]
        sum_w = sum(n / sigma2_pool for k, n in cell_ns.items() if n >= MIN_CELL_N_RELIABLE)
        sum_w2 = sum((n / sigma2_pool) ** 2 for k, n in cell_ns.items() if n >= MIN_CELL_N_RELIABLE)
        tau2 = max(0.0, (Q - df) / (sum_w - sum_w2 / sum_w)) if sum_w > 0 else 0.0
        reliable_shrunk = []
        for key, m in cell_means.items():
            n = cell_ns[key]
            if n < MIN_CELL_N_RELIABLE:
                continue
            lam = tau2 / (tau2 + sigma2_pool / n)
            shrunk = grand + lam * (m - grand)
            reliable_shrunk.append(shrunk)
        reliable_shrunk.sort()
        if len(reliable_shrunk) >= 3:
            t1 = reliable_shrunk[len(reliable_shrunk) // 3]
            t2 = reliable_shrunk[2 * len(reliable_shrunk) // 3]
        else:
            t1 = t2 = grand

        for key, m in cell_means.items():
            n = cell_ns[key]
            venue, surface, distance = key
            rho_goal_vals = cell_rho_goal.get(key, [])
            entry = {
                "venue": venue, "surface": surface, "distance": distance,
                "n": n, "rho_pos4_raw": round(m, 4),
                "rho_goal_raw": round(sum(rho_goal_vals) / len(rho_goal_vals), 4) if rho_goal_vals else None,
                "reliable": n >= MIN_CELL_N_RELIABLE,
            }
            if n >= MIN_CELL_N_RELIABLE:
                lam = tau2 / (tau2 + sigma2_pool / n)
                shrunk = grand + lam * (m - grand)
                entry["rho_pos4_shrunk"] = round(shrunk, 4)
                entry["tier"] = "低" if shrunk <= t1 else ("高" if shrunk >= t2 else "中")
            else:
                entry["rho_pos4_shrunk"] = None
                entry["tier"] = None
            cells_out.append(entry)
    else:
        for key, m in cell_means.items():
            n = cell_ns[key]
            venue, surface, distance = key
            rho_goal_vals = cell_rho_goal.get(key, [])
            cells_out.append({
                "venue": venue, "surface": surface, "distance": distance,
                "n": n, "rho_pos4_raw": round(m, 4),
                "rho_goal_raw": round(sum(rho_goal_vals) / len(rho_goal_vals), 4) if rho_goal_vals else None,
                "reliable": n >= MIN_CELL_N_RELIABLE, "rho_pos4_shrunk": None, "tier": None,
            })

    # 較正フィンガープリント(パラメータ変更でstaleになったことを検知するため)
    params_path = Path(__file__).resolve().parent / "mc_dyn_phase2_params.json"
    fp = hashlib.sha256(params_path.read_bytes()).hexdigest()[:16] if params_path.exists() else None

    payload = {
        "computed_scope": f"{START_DATE} 〜 (実行時点)",
        "n_races_used": len(results),
        "n_sim_per_race": N_SIM_VALIDATE,
        "grand_mean_rho_pos4": round(grand_mean_rho4, 4) if grand_mean_rho4 is not None else None,
        "grand_mean_rho_goal": round(grand_mean_rho_goal, 4) if grand_mean_rho_goal is not None else None,
        "sigma2_pool": round(sigma2_pool, 5),
        "heterogeneity_test": het,
        "heterogeneous": heterogeneous,
        "alpha": ALPHA,
        "min_cell_n_reliable": MIN_CELL_N_RELIABLE,
        "calibration_fingerprint": fp,
        "cells": cells_out,
    }
    out_path = Path(__file__).resolve().parent / "formation_accuracy.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"書き出し: {out_path}")
    print(f"異質性検定: Q={het['Q']:.2f} df={het['df']} p={het['p']:.4f}" if het else "異質性検定: 実施不可(信頼セル不足)")
    print(f"heterogeneous={heterogeneous}")


if __name__ == "__main__":
    main()
