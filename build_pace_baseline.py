"""
Phase 1b: PGR(Pace-Grit Rating, 展開粘り/届き指数) — Grit_H / Grit_S

「前で残った」「差しが届いた」を頭数統制した上で定量化する特徴量。

【頭数統制についての注記(2026-07-19発見の交絡への対応)】
統制なしで単純にpos4バケット×pace_typeで平均relative_finishを見ると、Sペース平均頭数
13.0頭 vs Hペース平均頭数14.9頭という交絡があり(本DBで実測: S=12.96頭, H=14.90頭で
ほぼ一致)、「スロー=前残り」等の定説とズレた見かけ上のパターンが出る
(頭数が少ないレースの方が前が残りやすい効果と、ペースによる位置取り有利/不利の効果が
混ざってしまう)。本実装では num_horses を粗いバケット(〜10/11-14/15+)として
ベースラインテーブルB[pos4バケット][pace_type][surface][num_horsesバケット]の
次元に追加することで統制する。

パイプライン:
  1. rel_finish = (finish-1)/(num_horses-1)  (0=1着, 1=最下位)
  2. pos4_bucket: 前(pos4/num_horses<=0.20) / 好位(<=0.45) / 中団(<=0.70) / 後方(それ以外)
  3. num_horses_bucket: '~10' / '11-14' / '15+'
  4. B[pos4_bucket, pace_type, surface, num_horses_bucket] = 平均rel_finish (母集団期待値)
  5. resid = B[cell] - rel_finish  (正 = 期待より良い着順で走った = 期待より粘った/届いた)
  6. Grit_H = (pos4_bucket in {前,好位}) かつ pace_type='H' の過去走residの加重平均
     Grit_S = (pos4_bucket in {中団,後方}) かつ pace_type='S' の過去走residの加重平均
     recency加重はCTAと同じ RECENCY_WEIGHTS を最大5件の該当走に適用。
     n<2は0(中立)、±0.25でキャップ。

  7. 馬別ペース応答の個体化:
     v_i(P) = 0.6 * STYLE_DEFAULTS[style][P] + 0.4 * rescale(horse_pace[P])
     style は generate_race_sim.classify_style()、horse_pace は
     pace_scenario.get_horse_pace_scores() を再利用する。
     rescale: 両者とも0-100スケール(NEUTRAL=60前後)のため、恒等変換(スケール調整不要)を
     デフォルトとしつつ、関数として切り出して将来調整可能にしておく。
"""
import sqlite3
from collections import defaultdict

import generate_race_sim as gsim
from generate_race_sim import STYLE_DEFAULTS
from pace_scenario import get_horse_pace_scores

DB_PATH = "keiba.db"

RECENCY_WEIGHTS = [1.0, 0.8, 0.6, 0.45, 0.35]  # build_class_par.RECENCY_WEIGHTS と同一設計
GRIT_CAP = 0.25
MIN_N_GRIT = 2


def rel_finish(finish, num_horses):
    if finish is None or num_horses is None or num_horses <= 1:
        return None
    return (finish - 1) / (num_horses - 1)


def pos4_bucket(pos4, num_horses):
    if pos4 is None or num_horses is None or num_horses <= 0 or pos4 <= 0:
        return None
    ratio = pos4 / num_horses
    if ratio <= 0.20:
        return "前"
    elif ratio <= 0.45:
        return "好位"
    elif ratio <= 0.70:
        return "中団"
    else:
        return "後方"


def num_horses_bucket(num_horses):
    if num_horses is None:
        return None
    if num_horses <= 10:
        return "~10"
    elif num_horses <= 14:
        return "11-14"
    else:
        return "15+"


# ── ベースラインテーブル構築 ─────────────────────────────────
MIN_N_BASELINE = 20  # B[...]セルの最低サンプル数


def build_baseline_table(conn, cutoff_date=None, min_n=MIN_N_BASELINE, verbose=True):
    """B[pos4_bucket, pace_type, surface, num_horses_bucket] = 平均rel_finish を構築。
    戻り値: {(pos4_bucket, pace_type, surface, nh_bucket): (mean_rel_finish, n)}
    """
    where = "WHERE r.finish IS NOT NULL AND r.finish < 90 AND r.num_horses > 1 AND r.pos4 IS NOT NULL"
    params = []
    if cutoff_date:
        where += " AND r.date < ?"
        params.append(cutoff_date)

    rows = conn.execute(f"""
        SELECT r.finish, r.num_horses, r.pos4, r.surface, rl.pace_type
        FROM results r
        JOIN race_laps rl ON rl.race_id = r.race_id
        {where}
          AND rl.pace_type IN ('H','M','S')
    """, params).fetchall()

    groups = defaultdict(list)
    for finish, num_horses, pos4, surface, pace_type in rows:
        pb = pos4_bucket(pos4, num_horses)
        nb = num_horses_bucket(num_horses)
        rf = rel_finish(finish, num_horses)
        if pb is None or nb is None or rf is None or surface not in ("芝", "ダ"):
            continue
        key = (pb, pace_type, surface, nb)
        groups[key].append(rf)

    baseline = {}
    for key, vals in groups.items():
        n = len(vals)
        if n < min_n:
            continue
        baseline[key] = (sum(vals) / n, n)

    if verbose:
        print(f"pace_baseline: {len(rows)}件から{len(baseline)}セル構築 (min_n={min_n})")
    return baseline


def save_baseline_table(conn, baseline, table_name="pace_baseline"):
    conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    conn.execute(f"""
        CREATE TABLE {table_name} (
          pos4_bucket TEXT, pace_type TEXT, surface TEXT, nh_bucket TEXT,
          mean_rel_finish REAL, n INTEGER,
          PRIMARY KEY (pos4_bucket, pace_type, surface, nh_bucket)
        )
    """)
    rows = [(pb, pt, surf, nb, mean_rf, n)
            for (pb, pt, surf, nb), (mean_rf, n) in baseline.items()]
    conn.executemany(f"""
        INSERT OR REPLACE INTO {table_name}
          (pos4_bucket, pace_type, surface, nh_bucket, mean_rel_finish, n)
        VALUES (?,?,?,?,?,?)
    """, rows)
    conn.commit()


# ── Grit_H / Grit_S 計算 ────────────────────────────────────
_H_BUCKETS = {"前", "好位"}
_S_BUCKETS = {"中団", "後方"}


def compute_horse_pgr(conn, horse_name, asof_date, baseline, lookback=20):
    """馬の過去走からGrit_H・Grit_Sを計算する。
    lookback: 遡って調べる直近レース数の上限(該当走がスパースなため、CTAより広めに取る)。
    戻り値: {'grit_h': float, 'grit_s': float, 'n_h': int, 'n_s': int}
    """
    rows = conn.execute("""
        SELECT r.date, r.finish, r.num_horses, r.pos4, r.surface, rl.pace_type
        FROM results r
        JOIN race_laps rl ON rl.race_id = r.race_id
        WHERE TRIM(r.horse_name) = TRIM(?) AND r.date < ?
          AND r.finish IS NOT NULL AND r.finish < 90
          AND rl.pace_type IN ('H','M','S')
        ORDER BY r.date DESC
        LIMIT ?
    """, (horse_name, asof_date, lookback)).fetchall()

    h_resids, s_resids = [], []
    for date, finish, num_horses, pos4, surface, pace_type in rows:
        pb = pos4_bucket(pos4, num_horses)
        nb = num_horses_bucket(num_horses)
        rf = rel_finish(finish, num_horses)
        if pb is None or nb is None or rf is None or surface not in ("芝", "ダ"):
            continue
        key = (pb, pace_type, surface, nb)
        if key not in baseline:
            continue
        mean_rf, n = baseline[key]
        resid = mean_rf - rf  # 正 = 期待より良い着順(=期待よりrel_finishが小さい)

        if pace_type == "H" and pb in _H_BUCKETS:
            h_resids.append(resid)
        elif pace_type == "S" and pb in _S_BUCKETS:
            s_resids.append(resid)

    def weighted_capped(resids):
        n = len(resids)
        if n < MIN_N_GRIT:
            return 0.0, n
        top = resids[:5]  # dateで既にDESC取得済みなので先頭が最新
        weights = RECENCY_WEIGHTS[:len(top)]
        val = sum(r * w for r, w in zip(top, weights)) / sum(weights)
        return max(-GRIT_CAP, min(GRIT_CAP, val)), n

    grit_h, n_h = weighted_capped(h_resids)
    grit_s, n_s = weighted_capped(s_resids)
    return {"grit_h": round(grit_h, 4), "grit_s": round(grit_s, 4), "n_h": n_h, "n_s": n_s}


# ── 馬別ペース応答の個体化 ───────────────────────────────────
def _rescale_horse_pace(v):
    """get_horse_pace_scores()の出力(0-100, NEUTRAL=60)をSTYLE_DEFAULTSと同スケールに
    揃える変換。両者ともほぼ同一スケール(0-100, 中立60前後)のため恒等変換とする。
    将来スケールがずれていることが分かれば、ここだけ調整すればよい設計。"""
    return v


def compute_horse_pace_response(conn, horse_name, asof_date, target_distance, surface,
                                 jockey=""):
    """v_i(P) = 0.6*STYLE_DEFAULTS[style][P] + 0.4*rescale(horse_pace[P]) をH/M/Sそれぞれ計算。
    戻り値: {'style': str, 'v': {'H':float,'M':float,'S':float}}
    """
    # gsim.get_horse_history()はconn.row_factory=sqlite3.Rowを前提とするため、
    # 呼び出し元の設定に関わらず動くようここで一時的に切り替えて元に戻す。
    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        history = gsim.get_horse_history(conn, horse_name, asof_date, surface)
    finally:
        conn.row_factory = prev_factory
    style = gsim.classify_style(history, target_distance, jockey=jockey, surface=surface)
    style_default = STYLE_DEFAULTS.get(style, STYLE_DEFAULTS["先行"])
    horse_pace = get_horse_pace_scores(horse_name, asof_date, surface, conn)

    v = {}
    for p in ("H", "M", "S"):
        v[p] = round(0.6 * style_default[p] + 0.4 * _rescale_horse_pace(horse_pace[p]), 2)
    return {"style": style, "v": v}


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    print("=== pace_baseline 構築(全期間、cutoff_date=None) ===")
    baseline = build_baseline_table(conn)
    save_baseline_table(conn, baseline)

    print("\n--- ベースライン内容(頭数統制の効果を確認) ---")
    for key in sorted(baseline.keys()):
        pb, pt, surf, nb = key
        mean_rf, n = baseline[key]
        print(f"  {pb:<4}{pt}{surf}{nb:<6}: mean_rel_finish={mean_rf:.4f} n={n}")

    print(f"\npace_baselineテーブル: "
          f"{conn.execute('SELECT COUNT(*) FROM pace_baseline').fetchone()[0]}行")
    conn.close()


if __name__ == "__main__":
    main()
