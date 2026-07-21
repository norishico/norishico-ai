"""venue×style（脚質）ボーナステーブルの構築
先行×長直線ボーナス（2026-07-21 /committee審議済み・のりお承認済み）

design: build_venue_sire_bonus.py と全く同じパターン(実測diff→スケーリング→ボーナス点、
cutoff_date対応の年度別リビルド、min_n/min_diff/min_stabilityフィルタ)を踏襲。
違いは venue×sire ではなく straight_bucket×style をキーにする点:
  - venue単独の二値ラベル(東京/新潟)ではなく、venue_geo_v2.straight_length_m(OSM実測の
    連続値、JRA公式直線距離)を3ビン(short/mid/long)に離散化して使う。
    cushion_sire_bonus が cushion値(連続)を soft/normal/firm の3ビンに離散化する
    パターンと同型。これによりTOKYO/新潟だけでなく、同程度の直線長を持つ阪神なども
    同じ"long"バケットに自然にプールされる。
  - 脚質は既存の _infer_running_style() と全く同じ閾値・ロジック(直近3走のpos4/num_horses
    比の平均、<=0.20逃げ/<=0.45先行/<=0.70中団/それ以外差追)で判定するが、350k行規模の
    一括構築のため scoring._infer_running_style() を1行ずつ呼ぶ代わりに同一ロジックを
    Pythonで一括計算する(build_gate_style_bias.pyが実レースpos4で同様の高速化をしている
    のと同じ思想。ただしこちらは直近走ベースなので _infer_running_style と完全に同じ値になる)。
  - 芝(surface='芝')のみ対象。straight_length_mはJRA公式の芝直線距離のため。
"""
import sqlite3
from collections import defaultdict, deque

DB_PATH = 'keiba.db'

# ── straight_length バケット閾値(m) ─────────────────────────
# venue_geo_v2実測値: 函館262/札幌266/福島292/小倉293/中山310/京都403.7/
#                     中京412.5/阪神473.6/東京525.9/新潟(loop)658.7/新潟(straight)1000
# short<300 / mid 300-450 / long>=450 で自然な3分割になる(境界に近い会場なし)。
SHORT_MAX = 300.0
MID_MAX = 450.0


def straight_bucket(length_m: float) -> str:
    if length_m is None:
        return None
    if length_m < SHORT_MAX:
        return 'short'
    if length_m < MID_MAX:
        return 'mid'
    return 'long'


def niigata_variant(surface, distance):
    """build_race_wind_v2.py と同一ロジック: 新潟のみ意味を持つ。芝1000m=直線コース。"""
    if surface == '芝' and distance == 1000:
        return 'straight'
    return 'loop'


def load_straight_lengths(conn):
    """{(venue, venue_variant): straight_length_m} を返す"""
    out = {}
    for r in conn.execute('SELECT venue, venue_variant, straight_length_m FROM venue_geo_v2'):
        out[(r[0], r[1])] = r[2]
    return out


def resolve_straight_length(venue, surface, distance, geo_map):
    variant = niigata_variant(surface, distance) if venue == '新潟' else 'default'
    return geo_map.get((venue, variant))


def _style_label(avg_ratio: float) -> str:
    if avg_ratio <= 0.20:
        return '逃げ'
    if avg_ratio <= 0.45:
        return '先行'
    if avg_ratio <= 0.70:
        return '中団'
    return '差追'


def compute_bulk_styles(conn, cutoff_date='2099-01-01', start_date=None):
    """全馬・全走について「直近3走(全サーフェス・全距離、date<現走日)」ベースの脚質を一括算出。
    scoring._infer_running_style() の閾値・履歴クエリと完全に同じロジック(月次キャッシュ由来の
    近似は行わず、行ごとに厳密計算)。

    Returns: list of dict (date, venue, surface, distance, horse_name, sire, finish, style)
             styleがNone(履歴なし)の行は除外済み。
    """
    date_lower = f"AND date >= '{start_date}'" if start_date else ''
    rows = conn.execute(f"""
        SELECT horse_name, date, venue, surface, distance, pos4, num_horses, finish,
               TRIM(sire) as sire
        FROM results
        WHERE date < ? {date_lower}
          AND finish IS NOT NULL AND finish > 0 AND finish < 90
        ORDER BY horse_name, date
    """, (cutoff_date,)).fetchall()

    out = []
    history = deque(maxlen=3)
    cur_horse = None
    for r in rows:
        h = r['horse_name']
        if h != cur_horse:
            cur_horse = h
            history = deque(maxlen=3)

        if history:
            ratios = [pos4 / nh for pos4, nh in history]
            style = _style_label(sum(ratios) / len(ratios))
            out.append({
                'date': r['date'], 'venue': r['venue'], 'surface': r['surface'],
                'distance': r['distance'], 'horse_name': h, 'sire': r['sire'],
                'finish': r['finish'], 'style': style,
            })

        if r['pos4'] and r['num_horses']:
            history.append((r['pos4'], r['num_horses']))

    return out


def build_venue_style_bonus(conn, cutoff_date='2099-01-01', start_date=None,
                             min_n=30, min_diff=12, min_stability=0.5):
    """straight_bucket×style のボーナステーブルを構築(venue_sire_bonusと同一パターン)。

    1. cutoff_date前の全データ(芝のみ)からstyle別の全体複勝率を計算
    2. cutoff_date前の全データからstraight_bucket×styleの複勝率を計算
    3. 乖離が大きく安定しているパターンにボーナスを付与
    """
    geo_map = load_straight_lengths(conn)
    styled = compute_bulk_styles(conn, cutoff_date=cutoff_date, start_date=start_date)

    # 芝のみ対象(straight_length_mはJRA公式の芝直線距離のため)
    turf = [r for r in styled if r['surface'] == '芝']
    for r in turf:
        r['straight_len'] = resolve_straight_length(r['venue'], r['surface'], r['distance'], geo_map)
        r['bucket'] = straight_bucket(r['straight_len'])
    turf = [r for r in turf if r['bucket'] is not None]

    if not turf:
        return 0

    # style別の全体複勝率(全バケット込み)
    style_overall = defaultdict(lambda: [0, 0])
    for r in turf:
        style_overall[r['style']][0] += 1
        if r['finish'] <= 3:
            style_overall[r['style']][1] += 1

    # bucket×style の年度別複勝率
    bs = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # {(bucket,style): {year: [n,t3]}}
    for r in turf:
        key = (r['bucket'], r['style'])
        yr = r['date'][:4]
        bs[key][yr][0] += 1
        if r['finish'] <= 3:
            bs[key][yr][1] += 1

    conn.execute('DROP TABLE IF EXISTS venue_style_bonus')
    conn.execute("""
        CREATE TABLE venue_style_bonus (
            straight_bucket TEXT, style TEXT,
            n INTEGER, bucket_t3r REAL, overall_t3r REAL, diff REAL,
            bonus REAL, stability REAL,
            PRIMARY KEY (straight_bucket, style)
        )
    """)

    count = 0
    for (bucket, style), yearly in bs.items():
        total_n = sum(v[0] for v in yearly.values())
        total_t3 = sum(v[1] for v in yearly.values())
        if total_n < min_n:
            continue
        bucket_t3r = total_t3 / total_n * 100

        on, ot3 = style_overall[style]
        if on < 20:
            continue
        overall_t3r = ot3 / on * 100

        diff = bucket_t3r - overall_t3r
        if diff < min_diff:
            continue

        years_data = [(yr, v) for yr, v in yearly.items() if v[0] >= 3]
        if len(years_data) < 2:
            continue
        years_ok = sum(1 for yr, v in years_data if v[1] / v[0] * 100 >= 25)
        stability = years_ok / len(years_data)
        if stability < min_stability:
            continue

        # venue_sire_bonus と同一スケーリング: +15pt乖離→+2pt, +25pt乖離→+4pt
        bonus = round(min(max(diff * 0.15, 1.5), 5.0), 1)

        conn.execute(
            'INSERT OR REPLACE INTO venue_style_bonus VALUES (?,?,?,?,?,?,?,?)',
            (bucket, style, total_n, round(bucket_t3r, 1), round(overall_t3r, 1),
             round(diff, 1), bonus, round(stability, 2))
        )
        count += 1

    conn.commit()
    print(f'  venue_style_bonus: {count}件構築 (cutoff={cutoff_date})')
    return count


if __name__ == '__main__':
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    build_venue_style_bonus(conn)

    print('\n=== venue_style_bonus テーブル(全期間ビルド、確認用) ===')
    for r in conn.execute('SELECT * FROM venue_style_bonus ORDER BY diff DESC'):
        print(f"  {r['straight_bucket']:>5} {r['style']:>4}  n={r['n']:>6} "
              f"bucket={r['bucket_t3r']:>5.1f}% overall={r['overall_t3r']:>5.1f}% "
              f"diff={r['diff']:>+5.1f} bonus={r['bonus']:>3.1f} stab={r['stability']:.0%}")
