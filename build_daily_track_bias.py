"""当日トラックバイアス推測テーブル構築

各レース日の同会場全レース結果から「内前有利/フラット/外差し有利」を判定。
日曜予想時に前日土曜(同会場)のbias_typeを参照して scoring を補正。

判定ロジック:
  各レースの1-3着馬について:
    is_inner = umaban / num_horses <= 0.4     (内枠)
    is_outer = umaban / num_horses >= 0.6     (外枠)
    is_front = pos4 / num_horses <= 0.4       (前/先行)
    is_late  = pos4 / num_horses >= 0.6       (差し/追込)

  日単位で集計:
    inner_rate - outer_rate + front_rate - late_rate = bias_score
    > +0.3 → inner_front (内前有利)
    < -0.3 → outer_late  (外差し有利)
    else   → flat

  最低4レース以上の日のみ判定 (かえでの保守主義)

テーブル: daily_track_bias (date, venue, bias_type, n_races, bias_score, inner_rate, outer_rate, front_rate, late_rate)
"""
import sqlite3
import sys


def build_daily_track_bias(conn, start_date='2021-01-01', cutoff_date='2099-01-01',
                           min_races=4, lo_pct=0.25, hi_pct=0.75):
    """daily_track_biasテーブルを全期間構築
    閾値はbias_score分布のパーセンタイル (lo_pct以下=outer_late, hi_pct以上=inner_front)
    日本競馬は構造的に内前有利(mean≈+0.47)なので絶対値ではなく相対分布で判定
    cutoff_date: リーク防止用。これ以降のデータは分布計算にも分類にも使わない
    """
    # 対象レース: 土日月の全開催分、1-3着馬 with umaban+pos4
    rows = conn.execute("""
        SELECT date, venue, race_num, finish, umaban, pos4, num_horses
        FROM results
        WHERE date >= ? AND date < ?
          AND finish IS NOT NULL AND finish >= 1 AND finish <= 3
          AND umaban IS NOT NULL AND umaban > 0
          AND pos4 IS NOT NULL AND pos4 > 0
          AND num_horses IS NOT NULL AND num_horses >= 8
        ORDER BY date, venue, race_num
    """, (start_date, cutoff_date)).fetchall()

    if not rows:
        print('  daily_track_bias: 対象データなし')
        return 0

    # (date, venue) 別に集計
    agg = {}  # {(date, venue): {'races': set, 'inner':[], 'outer':[], 'front':[], 'late':[]}}
    for r in rows:
        key = (r['date'], r['venue'])
        if key not in agg:
            agg[key] = {'races': set(), 'inner': 0, 'outer': 0,
                         'front': 0, 'late': 0, 'count': 0}
        a = agg[key]
        a['races'].add(r['race_num'])
        ratio_w = float(r['umaban']) / r['num_horses']
        ratio_p = float(r['pos4']) / r['num_horses']
        a['count'] += 1
        if ratio_w <= 0.4:
            a['inner'] += 1
        if ratio_w >= 0.6:
            a['outer'] += 1
        if ratio_p <= 0.4:
            a['front'] += 1
        if ratio_p >= 0.6:
            a['late'] += 1

    # テーブル再作成
    conn.execute("DROP TABLE IF EXISTS daily_track_bias")
    conn.execute("""
        CREATE TABLE daily_track_bias (
            date TEXT, venue TEXT, bias_type TEXT,
            n_races INTEGER, bias_score REAL,
            inner_rate REAL, outer_rate REAL,
            front_rate REAL, late_rate REAL,
            PRIMARY KEY (date, venue)
        )
    """)

    # まず全ての (date,venue) の bias_score を計算して分布を取得
    pre_rows = []
    for (date, venue), a in agg.items():
        n_races = len(a['races'])
        if n_races < min_races:
            continue
        total = a['count']
        if total == 0:
            continue
        inner_rate = a['inner'] / total
        outer_rate = a['outer'] / total
        front_rate = a['front'] / total
        late_rate = a['late'] / total
        bias_score = (inner_rate - outer_rate) + (front_rate - late_rate)
        pre_rows.append((date, venue, n_races, bias_score,
                         inner_rate, outer_rate, front_rate, late_rate))

    if not pre_rows:
        print('  daily_track_bias: 判定対象なし')
        return 0

    # 分布から閾値決定
    scores = sorted([r[3] for r in pre_rows])
    n = len(scores)
    lo_thresh = scores[int(n * lo_pct)]
    hi_thresh = scores[int(n * hi_pct)]
    print(f"  閾値決定: lo_pct={lo_pct:.0%}→{lo_thresh:.3f} / hi_pct={hi_pct:.0%}→{hi_thresh:.3f}")

    count_ins = count_flat = count_out = 0
    for (date, venue, n_races, bias_score,
         inner_rate, outer_rate, front_rate, late_rate) in pre_rows:
        if bias_score >= hi_thresh:
            bias_type = 'inner_front'
            count_ins += 1
        elif bias_score <= lo_thresh:
            bias_type = 'outer_late'
            count_out += 1
        else:
            bias_type = 'flat'
            count_flat += 1

        conn.execute(
            "INSERT OR REPLACE INTO daily_track_bias VALUES (?,?,?,?,?,?,?,?,?)",
            (date, venue, bias_type, n_races, round(bias_score, 3),
             round(inner_rate, 3), round(outer_rate, 3),
             round(front_rate, 3), round(late_rate, 3))
        )
    count_skip = 0

    conn.commit()
    total_inserted = count_ins + count_flat + count_out
    print(f"  daily_track_bias: {total_inserted}件構築 "
          f"(inner_front={count_ins} flat={count_flat} outer_late={count_out}, skip={count_skip})")
    return total_inserted


if __name__ == '__main__':
    conn = sqlite3.connect('keiba.db')
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.row_factory = sqlite3.Row

    build_daily_track_bias(conn)

    # サマリ統計
    stats = conn.execute("""
        SELECT bias_type, COUNT(*) as n,
               AVG(bias_score) as avg_score,
               MIN(bias_score) as min_score, MAX(bias_score) as max_score
        FROM daily_track_bias GROUP BY bias_type ORDER BY n DESC
    """).fetchall()
    print()
    print('=== daily_track_bias 集計サマリ ===')
    for s in stats:
        print(f"  {s['bias_type']:<12} n={s['n']:>4} "
              f"avg_score={s['avg_score']:+.2f} "
              f"[{s['min_score']:+.2f} ~ {s['max_score']:+.2f}]")

    # 年別分布
    print()
    print('=== 年別 inner_front vs outer_late ===')
    for r in conn.execute("""
        SELECT SUBSTR(date,1,4) as yr,
               SUM(CASE WHEN bias_type='inner_front' THEN 1 ELSE 0 END) as innerf,
               SUM(CASE WHEN bias_type='outer_late' THEN 1 ELSE 0 END) as outerl,
               SUM(CASE WHEN bias_type='flat' THEN 1 ELSE 0 END) as flat
        FROM daily_track_bias GROUP BY yr ORDER BY yr
    """):
        print(f"  {r['yr']}: inner_front={r['innerf']:>3} flat={r['flat']:>3} outer_late={r['outerl']:>3}")
    conn.close()
