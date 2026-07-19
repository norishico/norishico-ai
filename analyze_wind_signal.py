"""
Phase 0d: Gate 0 分析
「風データ×展開シミュレーション」リサーチラインの継続可否を判定する一発チェック。

1. 回帰分析: last_3f_race ~ tail_home + venue_FE + distance_bucket_FE
             + track_cond_FE + class_FE + surface_FE
   |t値| < 2 なら「風トラックは信号なし」と判定。
2. クロス集計: tail_homeを三分位に分け、脚質別(pos4/num_horses比で簡易判定)の
             複勝率(3着内率)を集計。
3. first_3f についても同様の回帰。

【重要 / リーク防止に関する注記】
本スクリプトはPhase 0の一発探索的チェックであり、全期間データ(2019-2025)を
まとめて使って良い(実運用のROI検証ではない)。ただし、もし本ラインが
Phase 1以降に進む場合、ここで得られる係数(tail_homeの効果量等)を実運用の
スコアリングやフィルタに使うには、CLAUDE.md/committee運用ルールに従い
年次Walk-Forward CV(訓練年と検証年を分離した再学習)が必須である。
このスクリプトの回帰係数をそのままプロダクションに転用しないこと。

numpyのみでOLS(最小二乗法)+t値を計算する(statsmodels/sklearn未導入のため)。
固定効果は各カテゴリ変数を加法的ダミー変数(drop_first)として投入する
(venue×distance×track_cond×class のフル交互作用ではなく加法モデル)。
"""
import sqlite3
import re
import math
import numpy as np
import pandas as pd

DB_PATH = "keiba.db"


def infer_class_bucket(race_name):
    if not race_name:
        return "unknown"
    if "新馬" in race_name:
        return "shinba"
    if "未勝利" in race_name:
        return "mishoi"
    if "１勝" in race_name or "1勝" in race_name:
        return "1shou"
    if "２勝" in race_name or "2勝" in race_name:
        return "2shou"
    if "３勝" in race_name or "3勝" in race_name:
        return "3shou"
    if re.search(r"[Gg][123]|Ｇ[１２３]", race_name):
        return "grade"
    if "(L)" in race_name or "（Ｌ）" in race_name or race_name.strip().endswith("L"):
        return "listed"
    return "open"


def infer_style(pos4, num_horses):
    if pos4 is None or num_horses is None or num_horses <= 0 or pos4 <= 0:
        return None
    ratio = pos4 / num_horses
    if ratio <= 0.20:
        return "逃げ"
    elif ratio <= 0.45:
        return "先行"
    elif ratio <= 0.70:
        return "中団"
    else:
        return "差追"


def distance_bucket(d):
    if d is None:
        return "unknown"
    if d < 1400:
        return "sprint(<1400)"
    if d < 1800:
        return "mile(1400-1799)"
    if d < 2200:
        return "intermediate(1800-2199)"
    if d < 2600:
        return "long(2200-2599)"
    return "extra(2600+)"


def ols_with_t(y, X_df):
    """y: pd.Series, X_df: pd.DataFrame(数値+ダミー列済み, 定数項含まず)
    戻り値: DataFrame(coef, se, t) で index=X_df.columns + 'const'"""
    X = np.column_stack([np.ones(len(X_df)), X_df.values.astype(float)])
    yv = y.values.astype(float)

    # 最小二乗(擬似逆行列; ランク落ちにも頑健)
    XtX_pinv = np.linalg.pinv(X.T @ X)
    beta = XtX_pinv @ X.T @ yv

    resid = yv - X @ beta
    n, k = X.shape
    dof = max(n - k, 1)
    sigma2 = (resid @ resid) / dof

    var_beta = sigma2 * XtX_pinv
    se = np.sqrt(np.clip(np.diag(var_beta), 0, None))
    t = np.divide(beta, se, out=np.zeros_like(beta), where=se > 0)

    names = ["const"] + list(X_df.columns)
    return pd.DataFrame({"coef": beta, "se": se, "t": t}, index=names), n, dof, sigma2


def build_dataset(conn):
    df = pd.read_sql_query("""
        SELECT rw.race_id, rw.date, rw.venue, rw.race_num, rw.tail_home,
               rw.wind_speed_avg, rw.wind_dir_avg_deg, rw.gust_max,
               rl.last_3f_race, rl.first_3f, rl.distance, rl.surface,
               r.track_cond, r.race_name
        FROM race_wind rw
        JOIN race_laps rl ON rl.race_id = rw.race_id
        LEFT JOIN (
            SELECT race_id, MIN(track_cond) AS track_cond, MIN(race_name) AS race_name
            FROM results GROUP BY race_id
        ) r ON r.race_id = rw.race_id
        WHERE rl.last_3f_race IS NOT NULL
    """, conn)

    df["class_bucket"] = df["race_name"].apply(infer_class_bucket)
    df["distance_bucket"] = df["distance"].apply(distance_bucket)
    df = df.dropna(subset=["tail_home", "last_3f_race", "venue", "track_cond"])
    df = df[df["track_cond"] != ""]
    return df


def run_regression(df, ycol, label):
    sub = df.dropna(subset=[ycol]).copy()
    dummies = pd.get_dummies(
        sub[["venue", "distance_bucket", "track_cond", "class_bucket", "surface"]],
        drop_first=True,
    )
    X = pd.concat([sub[["tail_home"]].reset_index(drop=True),
                    dummies.reset_index(drop=True)], axis=1)
    y = sub[ycol].reset_index(drop=True)

    result, n, dof, sigma2 = ols_with_t(y, X)
    tail_row = result.loc["tail_home"]

    print(f"\n=== 回帰: {ycol} ~ tail_home + venue/distance/track_cond/class/surface FE "
          f"({label}) ===")
    print(f"  n={n}, dof={dof}, resid_sigma={math.sqrt(sigma2):.4f}")
    print(f"  tail_home coef = {tail_row['coef']:.5f}  se={tail_row['se']:.5f}  "
          f"t = {tail_row['t']:.3f}")
    verdict = "信号あり(|t|>=2)" if abs(tail_row["t"]) >= 2 else "信号なし(|t|<2)"
    print(f"  判定: {verdict}")
    return tail_row["coef"], tail_row["t"], n


def cross_tab(conn, df):
    """tail_home三分位 x 脚質 の複勝率クロス集計"""
    runners = pd.read_sql_query("""
        SELECT r.race_id, r.pos4, r.num_horses, r.finish
        FROM results r
        WHERE r.date >= '2019-01-01' AND r.finish IS NOT NULL AND r.finish < 90
    """, conn)

    merged = runners.merge(
        df[["race_id", "tail_home"]].drop_duplicates("race_id"),
        on="race_id", how="inner"
    )
    merged["style"] = merged.apply(
        lambda r: infer_style(r["pos4"], r["num_horses"]), axis=1)
    merged = merged.dropna(subset=["style", "tail_home"])
    merged["fukusho"] = (merged["finish"] <= 3).astype(int)

    # 三分位
    q1, q2 = merged["tail_home"].quantile([1 / 3, 2 / 3])
    def tercile(v):
        if v <= q1:
            return "向かい風寄り"
        elif v <= q2:
            return "中立"
        else:
            return "追い風寄り"
    merged["wind_tercile"] = merged["tail_home"].apply(tercile)

    print(f"\n=== クロス集計: tail_home三分位(閾値 q1={q1:.2f}, q2={q2:.2f}) "
          f"x 脚質 -> 複勝率 ===")
    pivot = merged.groupby(["style", "wind_tercile"])["fukusho"].agg(["mean", "count"])
    pivot["mean"] = (pivot["mean"] * 100).round(2)
    print(pivot)

    print("\n--- 逃げ馬の複勝率: 向かい風寄り vs 追い風寄り ---")
    nige = merged[merged["style"] == "逃げ"]
    g = nige.groupby("wind_tercile")["fukusho"].agg(["mean", "count"])
    g["mean"] = (g["mean"] * 100).round(2)
    print(g)
    if "向かい風寄り" in g.index and "追い風寄り" in g.index:
        diff = g.loc["追い風寄り", "mean"] - g.loc["向かい風寄り", "mean"]
        print(f"\n逃げ馬 追い風寄り - 向かい風寄り 複勝率差 = {diff:+.2f}pt "
              f"(目安2pt以上で意味あり)")
    return merged


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    df = build_dataset(conn)
    print(f"race_wind x race_laps 結合データセット: {len(df)} レース")
    print(f"期間: {df['date'].min()} ~ {df['date'].max()}")
    print(f"venue分布:\n{df['venue'].value_counts()}")
    print(f"class_bucket分布:\n{df['class_bucket'].value_counts()}")

    coef1, t1, n1 = run_regression(df, "last_3f_race", "上がり3F")

    has_first3f = df["first_3f"].notna().sum()
    if has_first3f > 100:
        coef2, t2, n2 = run_regression(df, "first_3f", "前半3F")
    else:
        print(f"\nfirst_3f の非欠測数が少なすぎるためスキップ (n={has_first3f})")
        coef2, t2 = None, None

    merged = cross_tab(conn, df)

    print("\n" + "=" * 60)
    print("Gate 0 最終判定")
    print("=" * 60)
    if abs(t1) >= 2:
        print(f"last_3f_race回帰: |t|={abs(t1):.2f} >= 2 → 信号あり → 続行を検討")
    else:
        print(f"last_3f_race回帰: |t|={abs(t1):.2f} < 2 → 信号なし")
    if t2 is not None:
        if abs(t2) >= 2:
            print(f"first_3f回帰: |t|={abs(t2):.2f} >= 2 → 信号あり → 続行を検討")
        else:
            print(f"first_3f回帰: |t|={abs(t2):.2f} < 2 → 信号なし")

    conn.close()


if __name__ == "__main__":
    main()
