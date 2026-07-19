"""
Phase 0d-v2: Gate 0 分析(venue_geo_v2 / race_wind_v2 対応版)

v1(analyze_wind_signal.py)からの変更点:
  1. race_wind -> race_wind_v2 を参照(再測量済みbearing)。
  2. 会場別の効果分解(tail_home三分位 x 上がり3F)を明示的な関数として実装し、
     v1テーブル(race_wind)とv2テーブル(race_wind_v2)の両方で同じロジックを走らせ、
     再測量の前後比較(直線が長い会場ほど効果が大きい、という理論との整合性が
     改善したか)を直接出力する。
  3. 全体回帰(last_3f_race ~ tail_home + FE)もv1/v2両方で実行し、t値の変化を報告。

他のロジック(OLS実装、クラス/距離バケット推定等)はv1から変更なし。
"""
import sqlite3
import re
import math
import numpy as np
import pandas as pd

DB_PATH = "keiba.db"

# JRA公式の芝直線公称距離(参考、venue_geo_v2.straight_length_mと同じ値)
STRAIGHT_LEN_OFFICIAL = {
    "札幌": 266.1, "函館": 262.1, "福島": 292.0, "東京": 525.9,
    "中山": 310.0, "中京": 412.5, "京都": 403.7, "阪神": 473.6, "小倉": 293.0,
    "新潟(loop/外回り)": 658.7, "新潟(straight/直線)": 1000.0,
}


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
    X = np.column_stack([np.ones(len(X_df)), X_df.values.astype(float)])
    yv = y.values.astype(float)
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


def build_dataset(conn, race_wind_table):
    variant_col = "rw.venue_variant" if race_wind_table == "race_wind_v2" else "NULL AS venue_variant"
    df = pd.read_sql_query(f"""
        SELECT rw.race_id, rw.date, rw.venue, rw.race_num, rw.tail_home,
               rw.wind_speed_avg, rw.wind_dir_avg_deg, rw.gust_max, {variant_col},
               rl.last_3f_race, rl.first_3f, rl.distance, rl.surface,
               r.track_cond, r.race_name
        FROM {race_wind_table} rw
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

    # venue label for niigata split (v2 only)
    if race_wind_table == "race_wind_v2":
        def venue_label(row):
            if row["venue"] == "新潟":
                return "新潟(straight/直線)" if row["venue_variant"] == "straight" else "新潟(loop/外回り)"
            return row["venue"]
        df["venue_label"] = df.apply(venue_label, axis=1)
    else:
        df["venue_label"] = df["venue"]
    return df


def run_regression(df, ycol, label, fe_col="venue"):
    sub = df.dropna(subset=[ycol]).copy()
    dummies = pd.get_dummies(
        sub[[fe_col, "distance_bucket", "track_cond", "class_bucket", "surface"]],
        drop_first=True,
    )
    X = pd.concat([sub[["tail_home"]].reset_index(drop=True),
                    dummies.reset_index(drop=True)], axis=1)
    y = sub[ycol].reset_index(drop=True)

    result, n, dof, sigma2 = ols_with_t(y, X)
    tail_row = result.loc["tail_home"]

    print(f"\n=== 回帰: {ycol} ~ tail_home + {fe_col}/distance/track_cond/class/surface FE "
          f"({label}) ===")
    print(f"  n={n}, dof={dof}, resid_sigma={math.sqrt(sigma2):.4f}")
    print(f"  tail_home coef = {tail_row['coef']:.5f}  se={tail_row['se']:.5f}  "
          f"t = {tail_row['t']:.3f}")
    verdict = "信号あり(|t|>=2)" if abs(tail_row["t"]) >= 2 else "信号なし(|t|<2)"
    print(f"  判定: {verdict}")
    return tail_row["coef"], tail_row["t"], n


def venue_breakdown(df, label):
    """会場別: tail_home上位/下位三分位の平均last_3f_race差(会場内三分位、統制なし単純比較。
    背景クエリと同一の単純比較手法を再現。venue_labelで新潟はloop/straightに分離される)。"""
    print(f"\n=== 会場別 内訳(tail_home会場内三分位、統制なし単純比較) [{label}] ===")
    rows = []
    for venue_label, g in df.groupby("venue_label"):
        g = g.dropna(subset=["tail_home", "last_3f_race"])
        if len(g) < 30:
            continue
        q1, q2 = g["tail_home"].quantile([1 / 3, 2 / 3])
        low = g[g["tail_home"] <= q1]["last_3f_race"]
        high = g[g["tail_home"] >= q2]["last_3f_race"]
        if len(low) < 5 or len(high) < 5:
            continue
        diff = high.mean() - low.mean()  # 追い風寄り - 向かい風寄り (負 = 追い風で上がり3Fが速い = 理論通り)
        straight_len = STRAIGHT_LEN_OFFICIAL.get(venue_label)
        rows.append((venue_label, straight_len, len(g), diff, low.mean(), high.mean()))

    rows.sort(key=lambda r: (r[1] is None, r[1] if r[1] is not None else 0))
    print(f"{'venue':<20}{'straight_m':>11}{'n':>7}{'low(向風)avg':>14}{'high(追風)avg':>14}{'diff(高-低)':>12}")
    for venue_label, straight_len, n, diff, lowavg, highavg in rows:
        sl = f"{straight_len:.1f}" if straight_len is not None else "?"
        print(f"{venue_label:<20}{sl:>11}{n:>7}{lowavg:>14.3f}{highavg:>14.3f}{diff:>12.3f}")
    return rows


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    print("#" * 70)
    print("# BEFORE (v1: race_wind / venue_geo 旧測量値)")
    print("#" * 70)
    df1 = build_dataset(conn, "race_wind")
    print(f"race_wind(v1) x race_laps 結合データセット: {len(df1)} レース")
    coef1, t1, n1 = run_regression(df1, "last_3f_race", "上がり3F, v1")
    venue_breakdown(df1, "v1 旧測量値")

    print("\n" + "#" * 70)
    print("# AFTER (v2: race_wind_v2 / venue_geo_v2 再測量値 + 新潟直線/周回分離)")
    print("#" * 70)
    df2 = build_dataset(conn, "race_wind_v2")
    print(f"race_wind_v2(v2) x race_laps 結合データセット: {len(df2)} レース")
    coef2, t2, n2 = run_regression(df2, "last_3f_race", "上がり3F, v2", fe_col="venue")
    venue_breakdown(df2, "v2 再測量値+新潟分離")

    print("\n" + "=" * 70)
    print("全体回帰 t値の比較")
    print("=" * 70)
    print(f"  v1 (旧): tail_home coef={coef1:.5f}  t={t1:.3f}  n={n1}")
    print(f"  v2 (新): tail_home coef={coef2:.5f}  t={t2:.3f}  n={n2}")

    conn.close()


if __name__ == "__main__":
    main()
