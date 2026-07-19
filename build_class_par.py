"""
Phase 1a: CTA(クラス補正タイム能力指数, Class-adjusted Time Ability)

各馬の過去走タイムを「そのクラス・条件で通常どの程度のタイムが出るか」という基準
(class_par: 勝ちタイムの平均μ・標準偏差σ)でz-score化し、同日バイアス補正(既存の
race_level_index.same_day_biasを再利用)を施した上で、クラス梯子(L(cls)*k_cls)で
絶対化する。異なるクラス・条件をまたいだ「地力」の比較を可能にする指数。

パイプライン:
  1. classify_class(race_name): クラス分類(新表記1勝/2勝/3勝 + 旧表記500万下等に対応)
  2. build_class_par_table(): class_par(cls,venue,surface,distance,track_cond) = 勝ちタイムのμ,σ
  3. calibrate_k_cls(): 隣接クラス間の勝ちタイム中央値差をz換算し、surface別に1つのk_clsを較正
     (全期間データで良い。構造的パラメータであり年次WF不要と判断 — コーディネーター指示)
  4. compute_a_run(): 個々の過去走について A_run = L(cls)*k_cls - z_run_corrected を計算
  5. compute_horse_cta(): 馬ごとに直近5走のA_runをrecency加重し、CTA_main(上位2走)/
     CTA_full(全走)を算出

【リーク対策】
build_class_par_table() と calibrate_k_cls() はいずれも cutoff_date 引数を持ち、
指定時は date < cutoff_date のデータのみを使用する(実運用の年次WF CVでは
「その年の1/1」をcutoff_dateとして渡す想定)。今回はまだBTを実行しないため、
関数シグネチャとして用意するに留め、デフォルト(cutoff_date=None)は全期間データを使う。
"""
import sqlite3
import re
import math
from collections import defaultdict

DB_PATH = "keiba.db"

# ── クラス分類 ──────────────────────────────────────────────
# L(クラス序数): 新馬0/未勝利0.5/1勝1/2勝2/3勝3/OP・L 4/G3 4.5/G2 5/G1 6
CLASS_ORDER = ["新馬", "未勝利", "1勝", "2勝", "3勝", "OP", "L", "G3", "G2", "G1"]
L_MAP = {
    "新馬": 0.0, "未勝利": 0.5, "1勝": 1.0, "2勝": 2.0, "3勝": 3.0,
    "OP": 4.0, "L": 4.0, "G3": 4.5, "G2": 5.0, "G1": 6.0,
}


def classify_class(race_name):
    """race_nameからクラスを分類する。
    新表記(1勝/2勝/3勝クラス)・旧表記(500万下/1000万下/1600万下)の両方に対応。
    旧表記の対応関係(JRAの呼称変更、2019年前後に統一): 500万下=1勝クラス相当,
    1000万下=2勝クラス相当, 1600万下=3勝クラス相当。
    (DB確認: 500万下354件・1000万下89件が実在するため対応を追加。1600万下は0件だったが
     将来データ追加時のために念のため対応しておく)
    """
    rn = str(race_name or "")
    if not rn:
        return None
    if "新馬" in rn:
        return "新馬"
    if "未勝利" in rn:
        return "未勝利"
    if re.search(r"[Gg]1|Ｇ１", rn):
        return "G1"
    if re.search(r"[Gg]2|Ｇ２", rn):
        return "G2"
    if re.search(r"[Gg]3|Ｇ３", rn):
        return "G3"
    if "(L)" in rn or "（Ｌ）" in rn or rn.strip().endswith("L"):
        return "L"
    if "3勝" in rn or "３勝" in rn or "1600万" in rn:
        return "3勝"
    if "2勝" in rn or "２勝" in rn or "1000万" in rn:
        return "2勝"
    if "1勝" in rn or "１勝" in rn or "500万" in rn:
        return "1勝"
    if "障害" in rn:
        return None  # 障害レースはCTA対象外(平地と時計の意味が異なるため)
    if "オープン" in rn or re.search(r"\bOP\b", rn):
        return "OP"
    # 分類できない場合はOP扱い(review_app.py:_classify_raceの"OP/条件"方針を踏襲)
    return "OP"


# ── class_par テーブル構築 ──────────────────────────────────
MIN_N_CLASS_PAR = 5  # (cls,venue,surface,distance,track_cond) セルの最低勝ちタイム件数


def build_class_par_table(conn, cutoff_date=None, min_n=MIN_N_CLASS_PAR, verbose=True):
    """class_par(cls, venue, surface, distance, track_cond) = 勝ちタイムのμ,σ を構築。
    cutoff_date指定時は date < cutoff_date のデータのみ使用(リーク防止)。
    戻り値: {(cls,venue,surface,distance,track_cond): (mu, sigma, n)}
    """
    where = "WHERE finish = 1 AND time_sec IS NOT NULL AND time_sec > 0 AND surface IN ('芝','ダ')"
    params = []
    if cutoff_date:
        where += " AND date < ?"
        params.append(cutoff_date)

    rows = conn.execute(f"""
        SELECT race_name, venue, surface, distance, track_cond, time_sec
        FROM results
        {where}
    """, params).fetchall()

    groups = defaultdict(list)
    n_unclassified = 0
    for race_name, venue, surface, distance, track_cond, time_sec in rows:
        cls = classify_class(race_name)
        if cls is None or not track_cond:
            n_unclassified += 1
            continue
        key = (cls, venue, surface, distance, track_cond)
        groups[key].append(time_sec)

    class_par = {}
    for key, times in groups.items():
        n = len(times)
        if n < min_n:
            continue
        mu = sum(times) / n
        var = sum((t - mu) ** 2 for t in times) / n
        sigma = math.sqrt(var) if var > 0 else 0.01  # 0除算回避(件数少で全同値の場合など)
        class_par[key] = (mu, sigma, n)

    if verbose:
        print(f"class_par: {len(rows)}件の勝ち馬データから{len(class_par)}セル構築 "
              f"(未分類/条件欠損 {n_unclassified}件除外, min_n={min_n})")
    return class_par


def save_class_par_table(conn, class_par, table_name="class_par"):
    conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    conn.execute(f"""
        CREATE TABLE {table_name} (
          cls TEXT, venue TEXT, surface TEXT, distance INTEGER, track_cond TEXT,
          mu REAL, sigma REAL, n INTEGER,
          PRIMARY KEY (cls, venue, surface, distance, track_cond)
        )
    """)
    rows = [(cls, venue, surface, distance, track_cond, mu, sigma, n)
            for (cls, venue, surface, distance, track_cond), (mu, sigma, n) in class_par.items()]
    conn.executemany(f"""
        INSERT OR REPLACE INTO {table_name}
          (cls, venue, surface, distance, track_cond, mu, sigma, n)
        VALUES (?,?,?,?,?,?,?,?)
    """, rows)
    conn.commit()


# ── k_cls 較正 ──────────────────────────────────────────────
MIN_N_PAIR = 5  # 隣接クラス比較に必要な各クラス側の最低勝ちタイム件数


def calibrate_k_cls(conn, cutoff_date=None, min_n=MIN_N_PAIR, verbose=True):
    """隣接クラス間の勝ちタイム中央値差をz換算し、surface別に1つのk_clsを較正する。

    手順: 同一(venue,surface,distance,track_cond)内で隣接クラスの勝ちタイム中央値差
    (下位クラスの方が遅い=正)を、その2クラスの合成シグマで割ってz換算。
    L差(0.5または1.0)で割り"1 L単位あたりのz改善量"を推定し、(venue,surface,distance,
    track_cond)グループ×隣接クラスペアごとの推定値をサンプル数加重平均してsurface別の
    k_clsを1つ算出する。全期間データで1回較正(構造的パラメータのため年次WF不要、
    ただしcutoff_date指定時はそれ以前のデータのみで較正できるようにしておく)。
    """
    where = "WHERE finish = 1 AND time_sec IS NOT NULL AND time_sec > 0 AND surface IN ('芝','ダ')"
    params = []
    if cutoff_date:
        where += " AND date < ?"
        params.append(cutoff_date)

    rows = conn.execute(f"""
        SELECT race_name, venue, surface, distance, track_cond, time_sec
        FROM results
        {where}
    """, params).fetchall()

    groups = defaultdict(lambda: defaultdict(list))  # groups[(venue,surface,distance,track_cond)][cls] = [times]
    for race_name, venue, surface, distance, track_cond, time_sec in rows:
        cls = classify_class(race_name)
        if cls is None or not track_cond:
            continue
        groups[(venue, surface, distance, track_cond)][cls].append(time_sec)

    def median(vals):
        s = sorted(vals)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    k_estimates = defaultdict(list)  # surface -> [(k_estimate, weight), ...]
    for (venue, surface, distance, track_cond), by_cls in groups.items():
        for i in range(len(CLASS_ORDER) - 1):
            cls_lo, cls_hi = CLASS_ORDER[i], CLASS_ORDER[i + 1]
            if cls_lo not in by_cls or cls_hi not in by_cls:
                continue
            times_lo, times_hi = by_cls[cls_lo], by_cls[cls_hi]
            if len(times_lo) < min_n or len(times_hi) < min_n:
                continue
            med_lo, med_hi = median(times_lo), median(times_hi)
            time_diff = med_lo - med_hi  # 下位クラスの方が遅い想定なので正のはず
            if time_diff <= 0:
                continue  # 逆転(サンプルノイズ等)は除外
            pooled_sigma = (
                (sum((t - sum(times_lo) / len(times_lo)) ** 2 for t in times_lo)
                 + sum((t - sum(times_hi) / len(times_hi)) ** 2 for t in times_hi))
                / (len(times_lo) + len(times_hi))
            ) ** 0.5
            if pooled_sigma <= 0:
                continue
            l_diff = L_MAP[cls_hi] - L_MAP[cls_lo]
            if l_diff <= 0:
                continue  # OP/Lなど同一L値のペアはスキップ(0除算回避、較正に使えない)
            z_diff = time_diff / pooled_sigma
            k_est = z_diff / l_diff
            weight = min(len(times_lo), len(times_hi))
            k_estimates[surface].append((k_est, weight))

    k_cls = {}
    for surface, estimates in k_estimates.items():
        total_w = sum(w for _, w in estimates)
        k_cls[surface] = sum(k * w for k, w in estimates) / total_w if total_w > 0 else 0.3

    if verbose:
        for surface, estimates in k_estimates.items():
            vals = [k for k, w in estimates]
            print(f"k_cls[{surface}] = {k_cls[surface]:.4f} "
                  f"(推定ペア数={len(estimates)}, 個別推定値の範囲={min(vals):.3f}~{max(vals):.3f})")
    return k_cls


# ── A_run / CTA 計算 ────────────────────────────────────────
RECENCY_WEIGHTS = [1.0, 0.8, 0.6, 0.45, 0.35]


def compute_a_run(conn, class_par, k_cls, same_day_bias_map, race_name, venue, surface,
                   distance, track_cond, time_sec, date):
    """1走分の A_run = L(cls)*k_cls - z_run_corrected を計算。計算不能ならNoneを返す。"""
    cls = classify_class(race_name)
    if cls is None or cls not in L_MAP or not track_cond or time_sec is None or time_sec <= 0:
        return None
    key = (cls, venue, surface, distance, track_cond)
    if key not in class_par:
        return None
    mu, sigma, n = class_par[key]
    if sigma <= 0:
        return None
    z_run = (time_sec - mu) / sigma
    bias = same_day_bias_map.get((date, venue, surface), 0.0)
    z_run_corrected = z_run - bias
    k = k_cls.get(surface, 0.3)
    a_run = L_MAP[cls] * k - z_run_corrected
    return a_run


def load_same_day_bias_map(conn):
    """race_level_index.same_day_biasを(date,venue,surface)キーの辞書として読み込む。"""
    m = {}
    for date, venue, surface, bias in conn.execute(
        "SELECT DISTINCT date, venue, surface, same_day_bias FROM race_level_index"
    ):
        m[(date, venue, surface)] = bias
    return m


def compute_horse_cta(conn, horse_name, asof_date, class_par, k_cls, same_day_bias_map,
                       n_recent=5):
    """馬の直近n_recent走のA_runを取得し、CTA_main(上位2走加重平均)とCTA_full(全走加重平均)
    を返す。asof_date以降の走りは含めない(未来データ除外)。
    戻り値: {'cta_main': float or None, 'cta_full': float or None, 'n_runs': int,
             'a_runs': [(date, a_run), ...]}
    """
    rows = conn.execute("""
        SELECT date, race_name, venue, surface, distance, track_cond, time_sec
        FROM results
        WHERE TRIM(horse_name) = TRIM(?) AND date < ?
          AND finish IS NOT NULL AND finish < 90
        ORDER BY date DESC
        LIMIT ?
    """, (horse_name, asof_date, n_recent)).fetchall()

    a_runs = []
    for date, race_name, venue, surface, distance, track_cond, time_sec in rows:
        a = compute_a_run(conn, class_par, k_cls, same_day_bias_map,
                           race_name, venue, surface, distance, track_cond, time_sec, date)
        if a is not None:
            a_runs.append((date, a))

    if not a_runs:
        return {"cta_main": None, "cta_full": None, "n_runs": 0, "a_runs": []}

    weights = RECENCY_WEIGHTS[:len(a_runs)]

    # CTA_full: 全走加重平均
    cta_full = sum(a * w for (_, a), w in zip(a_runs, weights)) / sum(weights)

    # CTA_main: 上位2走(A_run値が高い=強い方から2つ)をそれぞれの元のrecency重みで加重平均
    idx_sorted_by_strength = sorted(range(len(a_runs)), key=lambda i: -a_runs[i][1])
    top2_idx = idx_sorted_by_strength[:2]
    top2_weight_sum = sum(weights[i] for i in top2_idx)
    cta_main = sum(a_runs[i][1] * weights[i] for i in top2_idx) / top2_weight_sum

    return {"cta_main": round(cta_main, 4), "cta_full": round(cta_full, 4),
            "n_runs": len(a_runs), "a_runs": a_runs}


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    print("=== class_par 構築(全期間、cutoff_date=None) ===")
    class_par = build_class_par_table(conn)
    save_class_par_table(conn, class_par)

    print("\n=== k_cls 較正(全期間、surface別) ===")
    k_cls = calibrate_k_cls(conn)

    # k_clsもDBに保存(再利用しやすいように)
    conn.execute("DROP TABLE IF EXISTS class_k")
    conn.execute("CREATE TABLE class_k (surface TEXT PRIMARY KEY, k_cls REAL)")
    conn.executemany("INSERT INTO class_k VALUES (?,?)", list(k_cls.items()))
    conn.commit()

    print(f"\nclass_parテーブル: {conn.execute('SELECT COUNT(*) FROM class_par').fetchone()[0]}行")
    print(f"class_kテーブル: {dict(conn.execute('SELECT * FROM class_k').fetchall())}")

    conn.close()


if __name__ == "__main__":
    main()
