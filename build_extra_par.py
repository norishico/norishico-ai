"""
Phase 3: rfa_rank_z / rfa_margin_z / l3f_z 用の較正テーブル構築。

- rank_par: 単一グローバルmu/sigma(着順ベースrel_finishの符号反転値の分布)。cutoff_date対応。
- margin_par: (surface, dist_bucket)10セルのsigma(平均センタリングなし、k_cls同格の構造的パラメータ)。
- l3f_par: (cls, venue, surface, distance, track_cond) — class_parと同一セル構造・同一較正パターン。
"""
import sqlite3
import math
from collections import defaultdict

from build_class_par import classify_class

DB_PATH = "keiba.db"


def dist_bucket(d):
    if d is None:
        return None
    if d <= 1200:
        return "~1200"
    elif d <= 1600:
        return "~1600"
    elif d <= 2000:
        return "~2000"
    elif d <= 2400:
        return "~2400"
    else:
        return "2401+"


def rel_finish_neg(finish, num_horses):
    """-(finish-1)/(num_horses-1). 0=1着(最良), -1=最下位。"""
    if finish is None or num_horses is None or num_horses <= 1:
        return None
    return -(finish - 1) / (num_horses - 1)


def build_rank_par(conn, cutoff_date=None, verbose=True):
    """rel_finish_negのグローバル(mu,sigma)。"""
    where = "WHERE finish IS NOT NULL AND finish < 90 AND num_horses > 1"
    params = []
    if cutoff_date:
        where += " AND date < ?"
        params.append(cutoff_date)
    vals = []
    for finish, num_horses in conn.execute(
        f"SELECT finish, num_horses FROM results {where}", params
    ):
        v = rel_finish_neg(finish, num_horses)
        if v is not None:
            vals.append(v)
    mu = sum(vals) / len(vals)
    var = sum((v - mu) ** 2 for v in vals) / len(vals)
    sigma = math.sqrt(var) if var > 0 else 0.01
    if verbose:
        print(f"rank_par: n={len(vals)} mu={mu:.4f} sigma={sigma:.4f}")
    return mu, sigma


def build_margin_par(conn, cutoff_date=None, min_n=30, verbose=True):
    """(surface,dist_bucket) 10セルのsigma(平均センタリングなし)。
    1着自身の行のmarginもそのまま使う(DB格納値=次点との差の負値)。
    margin>=90(DNFセンチネル999.9)またはNULLはスキップ。"""
    where = "WHERE margin IS NOT NULL AND margin < 90 AND surface IN ('芝','ダ')"
    params = []
    if cutoff_date:
        where += " AND date < ?"
        params.append(cutoff_date)
    groups = defaultdict(list)
    for surface, distance, margin in conn.execute(
        f"SELECT surface, distance, margin FROM results {where}", params
    ):
        db = dist_bucket(distance)
        if db is None:
            continue
        clipped = max(-2.0, min(4.0, margin))
        groups[(surface, db)].append(clipped)

    par = {}
    for key, vals in groups.items():
        n = len(vals)
        if n < min_n:
            continue
        # 平均センタリングなし: sigmaは0中心からの二乗平均平方根(RMS)として算出
        rms = math.sqrt(sum(v ** 2 for v in vals) / n)
        par[key] = (rms if rms > 0 else 0.5, n)
    if verbose:
        print(f"margin_par: {len(par)}セル構築")
        for k, (s, n) in sorted(par.items()):
            print(f"  {k}: sigma={s:.4f} n={n}")
    return par


def save_margin_par(conn, par, table_name="margin_par"):
    conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    conn.execute(f"""
        CREATE TABLE {table_name} (
          surface TEXT, dist_bucket TEXT, sigma REAL, n INTEGER,
          PRIMARY KEY (surface, dist_bucket)
        )
    """)
    rows = [(surf, db, sigma, n) for (surf, db), (sigma, n) in par.items()]
    conn.executemany(f"INSERT OR REPLACE INTO {table_name} VALUES (?,?,?,?)", rows)
    conn.commit()


# ── l3f_par: class_parと同一セル構造・同一較正パターン ─────────────
MIN_N_L3F = 5


def build_l3f_par(conn, cutoff_date=None, min_n=MIN_N_L3F, verbose=True):
    """l3f_par(cls, venue, surface, distance, track_cond) = last3fのmu,sigma。
    class_par(勝ちタイムのみ)と異なり、全馬のlast3fを使う(過去走の一般的な脚力指標のため)。
    """
    where = "WHERE last3f IS NOT NULL AND last3f > 0 AND surface IN ('芝','ダ')"
    params = []
    if cutoff_date:
        where += " AND date < ?"
        params.append(cutoff_date)
    rows = conn.execute(f"""
        SELECT race_name, venue, surface, distance, track_cond, last3f
        FROM results {where}
    """, params).fetchall()

    groups = defaultdict(list)
    for race_name, venue, surface, distance, track_cond, last3f in rows:
        cls = classify_class(race_name)
        if cls is None or not track_cond:
            continue
        groups[(cls, venue, surface, distance, track_cond)].append(last3f)

    par = {}
    for key, vals in groups.items():
        n = len(vals)
        if n < min_n:
            continue
        mu = sum(vals) / n
        var = sum((v - mu) ** 2 for v in vals) / n
        sigma = math.sqrt(var) if var > 0 else 0.1
        par[key] = (mu, sigma, n)
    if verbose:
        print(f"l3f_par: {len(rows)}件から{len(par)}セル構築 (min_n={min_n})")
    return par


def save_l3f_par(conn, par, table_name="l3f_par"):
    conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    conn.execute(f"""
        CREATE TABLE {table_name} (
          cls TEXT, venue TEXT, surface TEXT, distance INTEGER, track_cond TEXT,
          mu REAL, sigma REAL, n INTEGER,
          PRIMARY KEY (cls, venue, surface, distance, track_cond)
        )
    """)
    rows = [(cls, venue, surface, distance, track_cond, mu, sigma, n)
            for (cls, venue, surface, distance, track_cond), (mu, sigma, n) in par.items()]
    conn.executemany(f"""
        INSERT OR REPLACE INTO {table_name} VALUES (?,?,?,?,?,?,?,?)
    """, rows)
    conn.commit()


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")

    print("=== rank_par ===")
    mu, sigma = build_rank_par(conn)
    conn.execute("DROP TABLE IF EXISTS rank_par")
    conn.execute("CREATE TABLE rank_par (mu REAL, sigma REAL)")
    conn.execute("INSERT INTO rank_par VALUES (?,?)", (mu, sigma))
    conn.commit()

    print("\n=== margin_par ===")
    mpar = build_margin_par(conn)
    save_margin_par(conn, mpar)

    print("\n=== l3f_par ===")
    lpar = build_l3f_par(conn)
    save_l3f_par(conn, lpar)

    conn.close()


if __name__ == "__main__":
    main()
