"""
Phase 0f: 中山異常値の追加検証(仮説A: コース混在／仮説B: コーナーのきつさ)

仮説Aは venue_curve_tightness.md に記載の通り、根拠(内外回り直線共有の再確認・
ダート方位角の新規測定)により統計検証を待たず却下(構造的な混在なし、venue_variant
追加分割の必要性なし)。

本スクリプトは仮説B(コーナーのきつさ)のみを統計検証する。
周長(circumference_m)を「コーナーのきつさ」の代理指標として使用(コーディネーター
承認済みの代替案。定量的なコーナー半径値は画像埋め込みのため取得できなかった)。

【重要: 分析の誠実性についての注記】
circumference_m・composite指標は venue_curve_tightness.md に事前に(この統計検証を
実行する前に)確定・保存した値である。統計結果を見てから数値を選び直していない。

Step A: 周長(コーナーのきつさの代理)と既知tail_home効果サイズの単純相関
Step B: last_3f_race ~ tail_home + circumference*tail_home + composite*tail_home
        + venue/distance/track_cond/class/surface FE の回帰
"""
import sqlite3
import re
import math
import numpy as np
import pandas as pd

DB_PATH = "keiba.db"

# venue_elevation.md で確定済み(仮説A/Bとも既に固定値として使用)
SLOPE_LAST3F = {
    "札幌": 0.0, "函館": 0.0, "福島": 0.0, "新潟(loop)": 0.0, "新潟(straight)": 0.0,
    "東京": 2.0, "中山": 2.2, "中京": 2.0, "京都": 0.0, "阪神": 1.9, "小倉": 0.6,
}

# venue_curve_tightness.md で確定済み(内外回りがある場は内回り=タイトな方を代表値に採用)
CIRCUMFERENCE_M = {
    "福島": 1600.0, "小倉": 1615.1, "新潟(loop)": 1623.0, "新潟(straight)": 1623.0,
    "函館": 1626.6, "札幌": 1640.9, "中山": 1667.1, "中京": 1705.9,
    "阪神": 1689.0, "京都": 1782.8, "東京": 2083.1,
}

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
    print("Step A: 周長(コーナーのきつさ代理指標)と既知tail_home効果サイズの単純相関")
    print("=" * 70)
    venues = list(KNOWN_EFFECT.keys())
    circ = [CIRCUMFERENCE_M[v] for v in venues]
    slope = [SLOPE_LAST3F[v] for v in venues]
    tightness = [1.0 / c for c in circ]
    composite = [s * t * 1000 for s, t in zip(slope, tightness)]
    effects = [KNOWN_EFFECT[v] for v in venues]

    print(f"{'venue':<16}{'circ_m':>9}{'slope_m':>9}{'composite':>11}{'effect':>9}")
    for v, c, s, cp, e in sorted(zip(venues, circ, slope, composite, effects), key=lambda t: t[4]):
        print(f"{v:<16}{c:>9.1f}{s:>9.1f}{cp:>11.3f}{e:>9.3f}")

    r_circ = pearson(circ, effects)
    r_comp = pearson(composite, effects)
    n = len(venues)
    t_circ = r_circ * math.sqrt(n - 2) / math.sqrt(1 - r_circ ** 2) if abs(r_circ) < 1 else float("inf")
    t_comp = r_comp * math.sqrt(n - 2) / math.sqrt(1 - r_comp ** 2) if abs(r_comp) < 1 else float("inf")
    print(f"\n周長 vs 効果:      r={r_circ:.3f}, t={t_circ:.3f} "
          f"(仮説: 周長が小さい=タイトなほど効果が大きい→負の相関を予想)")
    print(f"複合指標 vs 効果:  r={r_comp:.3f}, t={t_comp:.3f} "
          f"(仮説: 坂×タイトさが大きいほど効果が大きい→正の相関を予想)")
    print("(n=10, dof=8。参考値に留める)")
    return r_circ, r_comp


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
    df["circumference_m"] = df["venue_label"].map(CIRCUMFERENCE_M)
    df["slope_last3f"] = df["venue_label"].map(SLOPE_LAST3F)
    df["tightness"] = 1.0 / df["circumference_m"]
    df["composite"] = df["slope_last3f"] * df["tightness"] * 1000
    return df


def step_b_regression(df):
    print("\n" + "=" * 70)
    print("Step B: last_3f_race ~ tail_home + tightness*tail_home + composite*tail_home + FE")
    print("=" * 70)
    sub = df.dropna(subset=["last_3f_race", "tightness", "composite"]).copy()
    sub["tightness_x_tailhome"] = sub["tightness"] * sub["tail_home"] * 1000  # scale for readability
    sub["composite_x_tailhome"] = sub["composite"] * sub["tail_home"]

    dummies = pd.get_dummies(
        sub[["venue_label", "distance_bucket", "track_cond", "class_bucket", "surface"]],
        drop_first=True,
    )
    X = pd.concat([
        sub[["tail_home", "tightness_x_tailhome", "composite_x_tailhome"]].reset_index(drop=True),
        dummies.reset_index(drop=True),
    ], axis=1)
    y = sub["last_3f_race"].reset_index(drop=True)

    result, n, dof, sigma2 = ols_with_t(y, X)
    print(f"n={n}, dof={dof}, resid_sigma={math.sqrt(sigma2):.4f}")
    for name in ["tail_home", "tightness_x_tailhome", "composite_x_tailhome"]:
        row = result.loc[name]
        print(f"  {name:<24} coef={row['coef']:.6f}  se={row['se']:.6f}  t={row['t']:.3f}")

    t_tight = result.loc["tightness_x_tailhome", "t"]
    t_comp = result.loc["composite_x_tailhome", "t"]
    print(f"\n  tightness×tail_home 判定: "
          f"{'交互作用あり(|t|>=2)' if abs(t_tight) >= 2 else '交互作用の証拠なし(|t|<2)'}")
    print(f"  composite×tail_home 判定: "
          f"{'交互作用あり(|t|>=2)' if abs(t_comp) >= 2 else '交互作用の証拠なし(|t|<2)'}")
    return t_tight, t_comp


def main():
    step_a_correlation()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")
    df = build_dataset(conn)
    print(f"\n(回帰用データセット: {len(df)} レース)")
    t_tight, t_comp = step_b_regression(df)

    print("\n" + "=" * 70)
    print("最終まとめ")
    print("=" * 70)
    print(f"tightness×tail_home 交互作用t値: {t_tight:.3f}")
    print(f"composite×tail_home 交互作用t値: {t_comp:.3f}")
    conn.close()


if __name__ == "__main__":
    main()
