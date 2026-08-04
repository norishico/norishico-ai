# -*- coding: utf-8 -*-
"""
build_classify_style_c2_model.py — classify_style_c2 用モデルバンドルの構築

C2a-ord(委員会承認済み、2026-08-03採用決定)の推論に必要な全アセットを
1つのpklバンドル `classify_style_c2_model.pkl` にまとめる。

同梱物:
  - v6full : 履歴あり馬用ロジット(34特徴量) + fb2ロジット(履歴ゼロ馬用21特徴量)
             出所: improve_classify_style_v2c_model.pkl (pair_full)
             学習データ: 2021-01-01..2023-12-31 (TRAIN期間、v2cで学習・凍結)
  - ctx_fb : レース文脈構築用fbロジット(8特徴量)
             出所: improve_classify_style_v2b_model.pkl (fb_clf/fb_scaler)
             学習データ: 同TRAIN期間
  - priors : 種牡馬/騎手×表面の平均通過順位比率(縮小事前分布用)
             本スクリプト実行時にkeiba.dbから date < prior_cutoff で集計
  - meta   : 生成日・出所・検証成績・再学習方針(未定)

検証成績(improve_classify_style_v2d.py compare4):
  VALID 2024-2025: acc 45.26% / macro 40.36%
  2026HO(1-7月) : acc 45.93% / macro 41.14%

再学習頻度・担当: 未定(のりおさんが後日決定。勝手に運用ルールを決めないこと)

使い方:
  py -3 build_classify_style_c2_model.py                     # cutoff=今日(本番用)
  py -3 build_classify_style_c2_model.py --prior-cutoff 2024-01-01 --out test.pkl
"""
import argparse
import pickle
import sqlite3
from datetime import date

DB_PATH = "keiba.db"
V2B_MODEL = "improve_classify_style_v2b_model.pkl"
V2C_MODEL = "improve_classify_style_v2c_model.pkl"
OUT_DEFAULT = "classify_style_c2_model.pkl"


def build_priors_sql(conn, cutoff):
    """improve_classify_style_v2b.build_priors と同一条件のSQL集計。
    sire → (n, mean_ratio) / (jockey, surface) → (n, mean_ratio)"""
    sire_p = {}
    for s, n, mean in conn.execute("""
        SELECT TRIM(sire), COUNT(*), AVG(pos4 * 1.0 / num_horses)
        FROM results
        WHERE date >= '2019-01-01' AND date < ? AND finish < 90
          AND pos4 IS NOT NULL AND pos4 > 0 AND num_horses > 1
          AND sire IS NOT NULL AND TRIM(sire) != ''
        GROUP BY TRIM(sire)
    """, (cutoff,)):
        sire_p[s] = (n, mean)
    jockey_p = {}
    for j, sf, n, mean in conn.execute("""
        SELECT jockey, surface, COUNT(*), AVG(pos4 * 1.0 / num_horses)
        FROM results
        WHERE date >= '2019-01-01' AND date < ? AND finish < 90
          AND pos4 IS NOT NULL AND pos4 > 0 AND num_horses > 1
          AND jockey IS NOT NULL AND jockey != ''
        GROUP BY jockey, surface
    """, (cutoff,)):
        jockey_p[(j, sf)] = (n, mean)
    return sire_p, jockey_p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prior-cutoff", default=None,
                    help="prior集計のカットオフ日(既定=今日。過去日を渡すと検証用)")
    ap.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args()
    cutoff = args.prior_cutoff or date.today().isoformat()

    with open(V2C_MODEL, "rb") as f:
        mdl3 = pickle.load(f)
    with open(V2B_MODEL, "rb") as f:
        mdl2 = pickle.load(f)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    sire_p, jockey_p = build_priors_sql(conn, cutoff)
    conn.close()

    bundle = {
        "meta": {
            "name": "classify_style_c2 (C2a-ord)",
            "generated": date.today().isoformat(),
            "train_period": "2021-01-01..2023-12-31",
            "prior_cutoff": cutoff,
            "source_scripts": [
                "improve_classify_style_v2.py", "improve_classify_style_v2b.py",
                "improve_classify_style_v2c.py", "improve_classify_style_v2d.py",
                "build_classify_style_c2_model.py",
            ],
            "validation": {
                "VALID_2024-2025": {"acc": 0.4526, "macro": 0.4036},
                "HO_2026_01-07": {"acc": 0.4593, "macro": 0.4114},
                "baseline_always_oikomi": {"VALID": 0.3967, "HO": 0.4001},
                "v0_classify_style": {"VALID": 0.3372, "HO": 0.3388},
            },
            "refresh_policy": "未定(のりおさんが後日決定)",
        },
        "config": {
            "decay": 0.8, "global_r": 0.47,
            "ords": {"逃げ": 0.0, "先行": 1.0, "差し": 2.0, "追い込み": 3.0},
            "hist_max_races": 15, "hist_max_days": 730,
            "sire_shrink_k": 30, "jockey_shrink_k": 50,
            "rhat_cap_n": 300, "rhat_base_k": 50,
        },
        "v6full": mdl3["pair_full"],   # {sh, ch, sf, cf, use_extras}
        "ctx_fb": {"scaler": mdl2["fb_scaler"], "clf": mdl2["fb_clf"]},
        "priors": {"sire": sire_p, "jockey": jockey_p},
    }
    with open(args.out, "wb") as f:
        pickle.dump(bundle, f)
    print(f"保存: {args.out}")
    print(f"  prior_cutoff={cutoff} sire={len(sire_p):,}件 jockey={len(jockey_p):,}件")


if __name__ == "__main__":
    main()
