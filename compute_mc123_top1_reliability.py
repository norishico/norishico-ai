# -*- coding: utf-8 -*-
"""
compute_mc123_top1_reliability.py — MC123の1位予想馬(ptop3最大)が実際に複勝圏内(3着以内)に
入る率について、会場×表面×距離セル別の信頼性を統計的に検定する(2026-08-10、委員会承認済み
設計の後追い実装)。

背景: 2026-08-09にanalyze_mc123_top1_conditions.pyで実データ分析(2024年以降n=7,303レース)を
行い、Sペース条件(単純な閾値ルール)はその夜のうちに表示実装した。会場×距離のセルは
n=1〜300超まで異質性が高く小標本セルのノイズ(100%/0%)が多発するため、compute_formation_accuracy.py
(隊列予測の会場×距離別精度チップ)で使ったのと同じCochranのQ検定+DerSimonian-Laird縮小+
n>=15閾値の3分位分けを適用してから表示する、という委員会合意を経て実装するもの。

方法(compute_formation_accuracy.pyとの違い):
  隊列精度(ρ、連続値)ではなく複勝的中(0/1、二値)のセル別比率が対象のため、個々のレースの
  01値そのものは保存されていない(analyze_mc123_top1_conditions.pyの出力はセル集計のみ)。
  二値比率のメタ分析における標準的な近似として、個々のレース(1件=1ベルヌーイ試行)の分散を
  全体プールした比率p_bar(1-p_bar)で近似する(セルごとの実際のp_i(1-p_i)ではなく、
  formation_accuracy.pyが「全セル共通の単一sigma2_pool」を使ったのと同じ簡略化方針を踏襲)。

入力: mc123_top1_conditions.json (analyze_mc123_top1_conditions.pyの出力、再利用・再計算なし)
出力: mc123_top1_reliability.json

使い方: py -3 compute_mc123_top1_reliability.py
"""
import json
import hashlib
from pathlib import Path

MIN_CELL_N_RELIABLE = 15
ALPHA = 0.05


def heterogeneity_test(cell_means, cell_ns, sigma2_pool):
    """Cochran's Q(固定効果版、compute_formation_accuracy.pyと同一式)。"""
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
    src_path = Path(__file__).resolve().parent / "mc123_top1_conditions.json"
    src = json.loads(src_path.read_text(encoding="utf-8"))
    cells_src = src["venue_distance_stats"]

    cell_means = {}
    cell_ns = {}
    for c in cells_src:
        key = (c["venue"], c["surface"], c["distance"])
        cell_means[key] = c["place_rate"]
        cell_ns[key] = c["n"]

    total_n = sum(cell_ns.values())
    total_placed = sum(cell_means[k] * cell_ns[k] for k in cell_means)
    p_bar = total_placed / total_n if total_n > 0 else None
    sigma2_pool = p_bar * (1 - p_bar) if p_bar is not None else 0.25

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
            entry = {"venue": venue, "surface": surface, "distance": distance,
                     "n": n, "place_rate_raw": round(m, 4),
                     "reliable": n >= MIN_CELL_N_RELIABLE}
            if n >= MIN_CELL_N_RELIABLE:
                lam = tau2 / (tau2 + sigma2_pool / n)
                shrunk = grand + lam * (m - grand)
                entry["place_rate_shrunk"] = round(shrunk, 4)
                entry["tier"] = "低" if shrunk <= t1 else ("高" if shrunk >= t2 else "中")
            else:
                entry["place_rate_shrunk"] = None
                entry["tier"] = None
            cells_out.append(entry)
    else:
        for key, m in cell_means.items():
            n = cell_ns[key]
            venue, surface, distance = key
            cells_out.append({"venue": venue, "surface": surface, "distance": distance,
                              "n": n, "place_rate_raw": round(m, 4),
                              "reliable": n >= MIN_CELL_N_RELIABLE,
                              "place_rate_shrunk": None, "tier": None})

    fp = hashlib.sha256(src_path.read_bytes()).hexdigest()[:16]

    payload = {
        "source": "mc123_top1_conditions.json",
        "source_generated_at": src.get("generated_at"),
        "n_races_used": src.get("n_races"),
        "overall_place_rate": round(p_bar, 4) if p_bar is not None else None,
        "sigma2_pool": round(sigma2_pool, 5),
        "heterogeneity_test": het,
        "heterogeneous": heterogeneous,
        "alpha": ALPHA,
        "min_cell_n_reliable": MIN_CELL_N_RELIABLE,
        "source_fingerprint": fp,
        "cells": cells_out,
    }
    out_path = Path(__file__).resolve().parent / "mc123_top1_reliability.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"書き出し: {out_path}")
    if het:
        print(f"異質性検定: Q={het['Q']:.2f} df={het['df']} p={het['p']:.4g}")
    else:
        print("異質性検定: 実施不可(信頼セル不足)")
    print(f"heterogeneous={heterogeneous}")
    if heterogeneous:
        n_high = sum(1 for c in cells_out if c["tier"] == "高")
        n_mid = sum(1 for c in cells_out if c["tier"] == "中")
        n_low = sum(1 for c in cells_out if c["tier"] == "低")
        print(f"信頼セル(n>=15) 高={n_high} 中={n_mid} 低={n_low} / 全{len(cells_out)}セル")


if __name__ == "__main__":
    main()
