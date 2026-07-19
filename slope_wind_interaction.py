"""
Phase 0e: 坂(高低差)×風効果の交互作用検証

背景: 中山(直線310m、JRA10場で2番目に短い部類)がtail_home効果最大(+0.779)という
「直線が長いほど効果が大きい」という理論と矛盾する異常値を示している。中山はゴール前
残り180-70m地点に高さ2.2m(最大勾配2.24%、JRA10場最大)の急坂があり、これが上がり3F
タイムへの風の影響を増幅している可能性がある、という仮説を検証する。

【重要: 分析の誠実性についての注記】
このスクリプトが使う坂の指標(SLOPE_LAST3F)は venue_elevation.md に事前に(この統計検証を
実行する前に)確定・保存した値である。統計結果を見てから坂データを都合よく選び直す、
という順序では絶対に行っていない。

Step A: 会場別tail_home効果サイズ(コーディネーターから提供された既知値)と坂指標の
        単純相関(ピアソン相関係数)を見る。
Step B: race_laps.last_3f_race ~ tail_home + slope_last3f*tail_home + venue/distance/
        track_cond/class/surface FE の回帰で交互作用項の有意性(t値)を確認する。
        (slope_last3f自体はvenue固定効果と完全に共線のため単独主効果は投入できない。
        交互作用項 slope_last3f * tail_home は venue内でtail_homeが変動するため
        識別可能。)
"""
import sqlite3
import re
import math
import numpy as np
import pandas as pd

DB_PATH = "keiba.db"

# venue_elevation.md で確定済みの「上がり3F区間(残り600m以内)の上り高さ(m)」
# 平坦/下り基調の会場は0とする(函館は直線が下り基調のため0扱い)。
SLOPE_LAST3F = {
    "札幌": 0.0,
    "函館": 0.0,
    "福島": 0.0,
    "新潟(loop)": 0.0,
    "新潟(straight)": 0.0,
    "東京": 2.0,
    "中山": 2.2,
    "中京": 2.0,
    "京都": 0.0,
    "阪神": 1.9,
    "小倉": 0.6,
}

# コーディネーターから提供された既知のtail_home効果サイズ(正=理論通りの方向、
# 大きいほど効果大)。venue_breakdownのdiffを符号反転した値に近いもの。
KNOWN_EFFECT = {
    "中山": 0.779, "札幌": 0.466, "函館": 0.464, "新潟(loop)": 0.463,
    "小倉": 0.318, "中京": 0.267, "東京": 0.261, "京都": 0.184,
    "阪神": 0.064, "福島": -0.002,
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


def venue_label(row):
    if row["venue"] == "新潟":
        return "新潟(straight)" if row["venue_variant"] == "straight" else "新潟(loop)"
    return row["venue"]


def pearson(x, y):
    x = np.array(x, dtype=float)
    y = np.array(y, dtype=float)
    return np.corrcoef(x, y)[0, 1]


def step_a_correlation():
    print("=" * 70)
    print("Step A: 坂の高さ(上がり3F区間内)と既知tail_home効果サイズの単純相関")
    print("=" * 70)
    venues = list(KNOWN_EFFECT.keys())
    slopes = [SLOPE_LAST3F[v] for v in venues]
    effects = [KNOWN_EFFECT[v] for v in venues]
    print(f"{'venue':<16}{'slope_m':>9}{'effect':>9}")
    for v, s, e in sorted(zip(venues, slopes, effects), key=lambda t: t[1]):
        print(f"{v:<16}{s:>9.1f}{e:>9.3f}")
    r = pearson(slopes, effects)
    n = len(venues)
    # t-test for pearson r significance
    t_r = r * math.sqrt(n - 2) / math.sqrt(1 - r ** 2) if abs(r) < 1 else float("inf")
    print(f"\nn={n}, ピアソン相関係数 r = {r:.3f}, t(r)={t_r:.3f}, dof={n-2}")
    print("(自由度8のt分布で|t|>=2.31が両側5%水準の目安。n=10と非常に小さいため参考値に留める)")
    return r, t_r


def build_dataset(conn):
    df = pd.read_sql_query("""
        SELECT rw.race_id, rw.date, rw.venue, rw.race_num, rw.tail_home, rw.venue_variant,
               rw.wind_speed_avg, rw.wind_dir_avg_deg, rw.gust_max,
               rl.last_3f_race, rl.first_3f, rl.distance, rl.surface,
               r.track_cond, r.race_name
        FROM race_wind_v2 rw
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
    df["venue_label"] = df.apply(venue_label, axis=1)
    df["slope_last3f"] = df["venue_label"].map(SLOPE_LAST3F)
    return df


def step_b_regression(df):
    print("\n" + "=" * 70)
    print("Step B: last_3f_race ~ tail_home + slope_last3f*tail_home + FE 回帰")
    print("=" * 70)
    sub = df.dropna(subset=["last_3f_race", "slope_last3f"]).copy()
    sub["slope_x_tailhome"] = sub["slope_last3f"] * sub["tail_home"]

    dummies = pd.get_dummies(
        sub[["venue_label", "distance_bucket", "track_cond", "class_bucket", "surface"]],
        drop_first=True,
    )
    X = pd.concat([
        sub[["tail_home", "slope_x_tailhome"]].reset_index(drop=True),
        dummies.reset_index(drop=True),
    ], axis=1)
    y = sub["last_3f_race"].reset_index(drop=True)

    result, n, dof, sigma2 = ols_with_t(y, X)
    print(f"n={n}, dof={dof}, resid_sigma={math.sqrt(sigma2):.4f}")
    print(f"\n  tail_home(主効果)         coef={result.loc['tail_home','coef']:.5f}  "
          f"se={result.loc['tail_home','se']:.5f}  t={result.loc['tail_home','t']:.3f}")
    print(f"  slope_last3f x tail_home  coef={result.loc['slope_x_tailhome','coef']:.5f}  "
          f"se={result.loc['slope_x_tailhome','se']:.5f}  t={result.loc['slope_x_tailhome','t']:.3f}")

    t_int = result.loc["slope_x_tailhome", "t"]
    verdict = "交互作用あり(|t|>=2)" if abs(t_int) >= 2 else "交互作用の証拠なし(|t|<2)"
    print(f"\n  判定: {verdict}")
    print("  (注: slope_last3fの単独主効果はvenue固定効果と完全共線のため推定不能。"
          "交互作用項のみが識別可能で、これは『坂がある会場ほどtail_homeの効きが強まるか』"
          "を検定している)")
    return t_int


def main():
    r, t_r = step_a_correlation()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")
    df = build_dataset(conn)
    print(f"\n(回帰用データセット: {len(df)} レース)")
    t_int = step_b_regression(df)

    print("\n" + "=" * 70)
    print("最終まとめ")
    print("=" * 70)
    print(f"Step A 単純相関: r={r:.3f} (n=10, 参考値)")
    print(f"Step B 回帰の交互作用項t値: {t_int:.3f}")
    conn.close()


if __name__ == "__main__":
    main()
