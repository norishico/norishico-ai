"""scoring_nar.py - 南関東競馬スコアリングエンジン
JRA版と独立した設計。training dataなし。
"""
import sqlite3
from pathlib import Path

PROJ = Path(__file__).parent.parent
DB_PATH = PROJ / 'nar_keiba.db'

# クラスランク (高いほど格上)
CLASS_RANK = {
    '新馬': 0,
    'C3': 1, 'C2': 2, 'C1': 3,
    'B3': 4, 'B2': 5, 'B1': 6,
    'A3': 7, 'A2': 8, 'A1': 9,
    'Open': 10, '特別': 10, '特選': 10,
    '重賞': 12, 'G3': 12, 'G2': 13, 'G1': 14,
    'Jpn3': 12, 'Jpn2': 13, 'Jpn1': 14,
}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA cache_size=-32768")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def get_warnings(conn, horse_name, race_info, before_date=None):
    """
    ルールベース反証フラグ (スコアに影響しない、表示専用)
    Returns: list of str (空リスト=WARNなし)
    """
    from datetime import date, timedelta
    warns = []
    if not before_date:
        before_date = race_info.get('date', '9999-99-99')

    # ── WARN1: 体重大幅増 ────────────────────────────────────
    entry_info = next(
        (e for e in race_info.get('entries', [])
         if e.get('horse_name', '').strip() == horse_name.strip()),
        None
    )
    if entry_info:
        bwd = entry_info.get('body_weight_diff')
        if bwd is not None and bwd >= 10:
            warns.append(f'体重+{int(bwd)}kg')

    # ── WARN2: 近走急下降 (直近3走で着順が連続悪化) ──────────
    recent = get_recent_results(conn, horse_name, before_date, n=3)
    if len(recent) >= 3:
        fins = [r[0] for r in recent[:3] if r[0] is not None]
        if len(fins) == 3 and fins[0] > fins[1] > fins[2]:
            warns.append(f'近走下降({fins[2]}→{fins[1]}→{fins[0]}着)')

    # ── WARN3: 長期休養明け (前走から90日以上) ───────────────
    if recent:
        last_date_str = recent[0][7]
        try:
            last_date = date.fromisoformat(last_date_str)
            race_date = date.fromisoformat(before_date) if isinstance(before_date, str) else before_date
            gap_days = (race_date - last_date).days
            if gap_days >= 90:
                warns.append(f'休養明け({gap_days}日)')
        except Exception:
            pass

    # ── WARN4: 同距離苦手 (±200m圏内で4走以上 かつ 大敗率60%以上) ──
    target_dist = race_info.get('distance', 0)
    if target_dist and recent:
        all_recent10 = get_recent_results(conn, horse_name, before_date, n=10)
        same_dist = [r for r in all_recent10
                     if r[5] and abs(r[5] - target_dist) <= 200
                     and r[0] is not None]
        if len(same_dist) >= 4:
            bad = sum(1 for r in same_dist if r[0] >= 8)
            if bad / len(same_dist) >= 0.6:
                warns.append(f'距離苦手({bad}/{len(same_dist)}走大敗, {target_dist}m)')

    # ── WARN5: キャリア最長距離更新 (+400m超) ───────────────────
    if target_dist and recent:
        all_recent20 = get_recent_results(conn, horse_name, before_date, n=20)
        prev_dists = [r[5] for r in all_recent20 if r[5]]
        if prev_dists:
            prev_max = max(prev_dists)
            if target_dist > prev_max + 400:
                warns.append(f'最長距離更新({prev_max}m→{target_dist}m)')

    return warns


def get_recent_results(conn, horse_name, before_date, n=10, max_days=730):
    """馬の直近N走成績を取得 (max_days以内のみ)"""
    from datetime import date, timedelta
    bd = date.fromisoformat(before_date) if isinstance(before_date, str) else before_date
    since = (bd - timedelta(days=max_days)).isoformat()
    rows = conn.execute("""
        SELECT r.finish, r.popularity, r.odds, r.umaban,
               rc.class_code, rc.distance, rc.venue_cd, rc.date, rc.heads_count,
               r.time_sec, COALESCE(rc.condition, '良') as condition,
               rc.venue_name, r.last_3f, r.margin
        FROM nar_results r
        JOIN nar_races rc ON r.race_id = rc.race_id
        WHERE r.horse_name = ?
          AND rc.date >= ?
          AND rc.date < ?
        ORDER BY rc.date DESC
        LIMIT ?
    """, (horse_name.strip(), since, before_date, n)).fetchall()
    return rows


_TIME_INDEX_CACHE = {}


def _load_time_index(conn):
    """nar_time_index テーブルを辞書として返す: (venue_name, distance, condition) -> (avg_t, std_t)
    プロセス内でキャッシュ（BT高速化のため）。"""
    global _TIME_INDEX_CACHE
    if _TIME_INDEX_CACHE:
        return _TIME_INDEX_CACHE
    try:
        rows = conn.execute(
            "SELECT venue_name, distance, condition, avg_t, std_t, avg_l3f, std_l3f FROM nar_time_index"
        ).fetchall()
        _TIME_INDEX_CACHE = {(v, d, c): (at, st, al, sl) for v, d, c, at, st, al, sl in rows}
    except Exception:
        _TIME_INDEX_CACHE = {}
    return _TIME_INDEX_CACHE


def _calc_time_index_score(recent, time_index):
    """
    タイム指数スコア v2: 「前走比のタイム指数上昇率」を評価する。
    絶対値ではなく変化量を見ることで、市場未織り込みの状態変化を捉える。

    計算:
      各走のz = (avg_t - horse_time) / std_t (正=速い)
      直近2走のdelta = z[0] - z[1] (最新 - 1走前)
      さらに直近1走の絶対zが平均以上(z>0)なら確認加点

    max: +1.5pt (上昇かつ直近速い)
    min: -1.0pt (急落かつ直近遅い)
    """
    # r indices: 0=finish,1=pop,2=odds,3=umaban,4=class,5=dist,6=venue_cd,
    #            7=date,8=heads,9=time_sec,10=condition,11=venue_name
    z_scores = []
    for r in recent[:3]:
        t = r[9]
        if not t or t <= 0:
            z_scores.append(None)
            continue
        key = (r[11], r[5], r[10])
        if key not in time_index:
            z_scores.append(None)
            continue
        avg_t, std_t = time_index[key][0], time_index[key][1]
        if std_t <= 0:
            z_scores.append(None)
            continue
        z_scores.append((avg_t - t) / std_t)

    valid = [(i, z) for i, z in enumerate(z_scores) if z is not None]
    if len(valid) < 1:
        return 0.0, {}

    latest_z = valid[0][1]

    # 前走比変化量 (2走以上あれば)
    delta = 0.0
    if len(valid) >= 2:
        delta = valid[0][1] - valid[1][1]  # 最新 - 1走前

    # スコア計算: 変化量ベース (±1pt) + 直近絶対値ボーナス (±0.5pt)
    delta_score = max(-1.0, min(1.0, delta * 0.8))
    abs_score = max(-0.5, min(0.5, latest_z * 0.3))
    score = delta_score + abs_score

    return round(score, 2), {
        'ti_latest_z': round(latest_z, 2),
        'ti_delta': round(delta, 2),
        'time_idx_score': round(score, 2),
    }


def _parse_margin(s):
    """着差文字列を馬身数(float)に変換する。変換不能は None。"""
    if not s:
        return None
    s = s.strip()
    if s in ('ハナ', '鼻', 'ハナ'):
        return 0.1
    if s in ('アタマ', '頭', 'アタマ'):
        return 0.2
    if s in ('クビ', '首', 'クビ'):
        return 0.3
    if s in ('大差', '大差'):
        return 10.0
    # "1.1/2" → 1.5 形式
    if '.' in s and '/' in s:
        try:
            int_part, frac = s.split('.', 1)
            n, d = frac.split('/')
            return int(int_part) + int(n) / int(d)
        except Exception:
            return None
    # "1/2" → 0.5 形式
    if '/' in s:
        try:
            n, d = s.split('/')
            return int(n) / int(d)
        except Exception:
            return None
    try:
        return float(s)
    except Exception:
        return None


def _calc_margin_score(recent):
    """
    着差スコア。負け時の惜敗度・勝ち時の強さを評価する。

    ロジック:
      1着で着差≥3馬身 → +0.3pt (強い勝ち方)
      2着以下で着差≤0.3馬身 → +0.5pt (惜敗、次走に期待)
      2着以下で着差0.3-1.0馬身 → +0.2pt
      2着以下で着差≥5馬身 → -0.5pt (大差負け)
    直近3走の加重平均(0.5/0.3/0.2)、max +0.8pt / min -0.5pt
    """
    # r indices: 0=finish,...,12=last_3f,13=margin
    scores = []
    for r in recent[:3]:
        finish = r[0]
        margin_str = r[13] if len(r) > 13 else None
        m = _parse_margin(margin_str) if margin_str else None

        if finish is None:
            scores.append(None)
            continue

        if finish == 1:
            scores.append(0.3 if (m is not None and m >= 3.0) else 0.0)
        else:
            if m is None:
                scores.append(None)
            elif m <= 0.3:
                scores.append(0.5)
            elif m <= 1.0:
                scores.append(0.2)
            elif m >= 5.0:
                scores.append(-0.5)
            else:
                scores.append(0.0)

    valid = [(i, s) for i, s in enumerate(scores) if s is not None]
    if not valid:
        return 0.0, {}

    weights = [0.5, 0.3, 0.2]
    total_w = sum(weights[i] for i, s in valid)
    weighted = sum(s * weights[i] for i, s in valid) / total_w
    score = max(-0.5, min(0.8, weighted))
    return round(score, 2), {'margin_score': round(score, 2)}


def _calc_last3f_score(recent, time_index):
    """
    上がり3Fスコア。
    直近3走の上がりタイムを会場×距離の平均と比較し、末脚の切れ味を評価する。
    z = (avg_l3f - horse_l3f) / std_l3f  (正=速い末脚)

    設計思想: 展開で不利でも末脚が速ければ次走の「隠れた期待値」がある。
    max: +2.0pt (直近の上がりが+1σ超)
    min: -1.0pt (直近の上がりが-1σ以下で連続)
    """
    # r indices: 0=finish,...,9=time_sec,10=condition,11=venue_name,12=last_3f
    z_scores = []
    for r in recent[:3]:
        l3f = r[12] if len(r) > 12 else None
        if not l3f or l3f <= 0:
            continue
        key = (r[11], r[5], r[10])
        if key not in time_index:
            continue
        entry = time_index[key]
        avg_l3f = entry[2] if len(entry) > 2 else None
        std_l3f = entry[3] if len(entry) > 3 else None
        if not avg_l3f or not std_l3f or std_l3f <= 0:
            continue
        z_scores.append((avg_l3f - l3f) / std_l3f)

    if not z_scores:
        return 0.0, {}

    # 直近走を重視した加重平均
    weights = [0.5, 0.3, 0.2][:len(z_scores)]
    total_w = sum(weights)
    weighted_z = sum(z * w for z, w in zip(z_scores, weights)) / total_w

    score = max(-0.5, min(0.8, weighted_z * 0.6))
    return round(score, 2), {
        'l3f_z': round(weighted_z, 2),
        'l3f_score': round(score, 2),
    }


def score_horse(conn, horse_name, race_info, before_date=None):
    """
    1頭のスコアを計算
    Returns: (score, breakdown_dict)
    """
    if not before_date:
        before_date = race_info.get('date', '9999-99-99')

    recent = get_recent_results(conn, horse_name, before_date, n=10)

    score = 0.0
    breakdown = {}

    if not recent:
        return score, {'note': '過去データなし'}

    # ── 1. 近走勝率スコア (直近5走) ──────────────────────
    last5 = recent[:5]
    wins5 = sum(1 for r in last5 if r[0] == 1)
    places5 = sum(1 for r in last5 if r[0] is not None and r[0] <= 3)
    n5 = len(last5)
    win_rate5 = wins5 / n5 if n5 > 0 else 0
    place_rate5 = places5 / n5 if n5 > 0 else 0

    win_score = win_rate5 * 4.0        # 0〜4pt
    place_score = place_rate5 * 2.0    # 0〜2pt
    score += win_score + place_score
    breakdown['win_rate5'] = round(win_rate5, 3)
    breakdown['place_rate5'] = round(place_rate5, 3)
    breakdown['form_score'] = round(win_score + place_score, 2)

    # ── 2. クラスドロップ/アップ ──────────────────────────
    current_class = race_info.get('class_code', '')
    current_rank = CLASS_RANK.get(current_class, 5)
    prev_classes = [r[4] for r in recent[:3] if r[4]]
    if prev_classes:
        prev_rank = CLASS_RANK.get(prev_classes[0], 5)
        class_diff = prev_rank - current_rank  # 正=降格=有利
        if class_diff >= 2:
            class_bonus = 1.5
        elif class_diff == 1:
            class_bonus = 0.8
        elif class_diff == 0:
            class_bonus = 0.0
        else:
            class_bonus = -0.5  # 昇格
        score += class_bonus
        breakdown['class_drop'] = class_diff
        breakdown['class_bonus'] = class_bonus

    # ── 3. 最近走人気 vs オッズ乖離 ──────────────────────
    # 人気3以内なのに2着3着が多い=堅実な馬
    top3_pops = [r[1] for r in last5 if r[1] is not None and r[1] <= 3]
    if top3_pops and places5 >= 2:
        score += 0.5
        breakdown['consistent_place'] = True

    # ── 4. 直近の成績トレンド ─────────────────────────────
    # 直近3走の着順平均 (小さいほど良い → 逆数スコア)
    finishes = [r[0] for r in recent[:3] if r[0] is not None]
    if finishes:
        avg_finish = sum(finishes) / len(finishes)
        # avg_finish=1.0→2pt、avg_finish=5.0→0pt
        trend_score = max(0, 2.0 - (avg_finish - 1) * 0.5)
        score += trend_score
        breakdown['avg_finish3'] = round(avg_finish, 2)
        breakdown['trend_score'] = round(trend_score, 2)

    # ── 5. 距離実績 ──────────────────────────────────────
    target_dist = race_info.get('distance', 0)
    if target_dist:
        dist_wins = [r for r in recent if r[5] and abs(r[5] - target_dist) <= 200 and r[0] == 1]
        dist_places = [r for r in recent if r[5] and abs(r[5] - target_dist) <= 200 and r[0] and r[0] <= 3]
        if dist_wins:
            score += 0.8
            breakdown['dist_win'] = len(dist_wins)
        elif dist_places:
            score += 0.3
            breakdown['dist_place'] = len(dist_places)

    # ── 6. 騎手の会場別勝率 ──────────────────────────────
    target_jockey = None
    for e in race_info.get('entries', []):
        if e.get('horse_name', '') == horse_name.strip():
            target_jockey = e.get('jockey', '')
            break

    if target_jockey:
        jockey_stats = conn.execute("""
            SELECT COUNT(*) as n, SUM(CASE WHEN r.finish=1 THEN 1 ELSE 0 END) as wins
            FROM nar_results r
            JOIN nar_races rc ON r.race_id = rc.race_id
            WHERE r.jockey = ? AND rc.venue_cd = ?
              AND rc.date >= DATE(?, '-1 year') AND rc.date < ?
        """, (target_jockey, race_info.get('venue_cd', ''), before_date, before_date)).fetchone()
        if jockey_stats and jockey_stats[0] >= 10:
            j_win_rate = jockey_stats[1] / jockey_stats[0]
            j_score = j_win_rate * 1.5  # 0〜1.5pt
            score += j_score
            breakdown['jockey_win_rate'] = round(j_win_rate, 3)
            breakdown['jockey_score'] = round(j_score, 2)

    # ── 8. 着差スコア (v3 step3) ──────────────────────────────────
    m_score, m_bkd = _calc_margin_score(recent)
    if m_score != 0.0:
        score += m_score
        breakdown.update(m_bkd)

    # ── 9. 上がり3Fスコア (v2 step2) ────────────────────────────
    time_index = _load_time_index(conn)
    l3f_score, l3f_bkd = _calc_last3f_score(recent, time_index)
    if l3f_score != 0.0:
        score += l3f_score
        breakdown.update(l3f_bkd)

    breakdown['total'] = round(score, 2)
    return score, breakdown


def score_race(race_info, conn=None):
    """
    1レース全馬スコア計算
    Returns: [{'horse_name':..., 'score':..., 'breakdown':..., ...}, ...]
    """
    if conn is None:
        conn = get_db()

    entries = race_info.get('entries', [])
    results = []
    for entry in entries:
        horse_name = entry.get('horse_name', '')
        sc, bkd = score_horse(conn, horse_name, race_info)

        results.append({
            'waku': entry.get('waku'),
            'umaban': entry.get('umaban'),
            'horse_name': horse_name,
            'sex': entry.get('sex'),
            'age': entry.get('age'),
            'weight_carried': entry.get('weight_carried'),
            'jockey': entry.get('jockey'),
            'body_weight': entry.get('body_weight'),
            'body_weight_diff': entry.get('body_weight_diff'),
            'odds': entry.get('odds'),
            'score': round(sc, 2),
            'breakdown': bkd,
        })

    results.sort(key=lambda x: x['score'], reverse=True)
    return results


def pick_honmei(scored_entries, min_score=5.5, min_odds=5.0, gap_req=2.0, max_odds=25.0,
                exclude_empty_class=True, exclude_c3=True, race_class_code='', venue_name='',
                max_gap_req=None):
    """
    本命選定: 最高スコア馬が条件を満たすか
    v3.3 (2026-04-28): score≥5.5, odds5-25, gap≥2.0, C3除外, 大井A/B系除外
    5年WF CV: N=192R, ROI 127.6%, worst=67.5%(2026)
    max_gap_req: チャレンジ枠用上限 (gap < max_gap_reqのみ通過)
    Returns: (honmei_dict, reason) or (None, reason)
    """
    if not scored_entries:
        return None, "出走馬なし"

    top = scored_entries[0]
    score = top['score']
    odds = top.get('odds') or 0

    # データなし馬が上位の場合はパス
    if top.get('breakdown', {}).get('note') == '過去データなし':
        return None, "過去データなし"

    # class_code空欄除外 (スコア信頼性低)
    if exclude_empty_class and not race_class_code:
        return None, "class_code不明"

    # C3クラス除外 (全会場で0%、構造的に難解)
    if exclude_c3 and race_class_code.startswith('C3'):
        return None, f"C3クラス除外 ({race_class_code})"

    # 大井専用: A系・B系クラス除外 (C1/C2中心、A2/B1/B2=0%、grid search最良でも85%)
    if venue_name == '大井' and race_class_code and \
       (race_class_code.startswith('A') or race_class_code.startswith('B')):
        return None, f"大井A/B系除外 ({race_class_code})"

    if score < min_score:
        return None, f"スコア不足 ({score:.1f} < {min_score})"
    if odds < min_odds:
        return None, f"オッズ低すぎ ({odds:.1f} < {min_odds})"
    if odds > max_odds:
        return None, f"オッズ高すぎ ({odds:.1f} > {max_odds})"

    # 2位との差
    if len(scored_entries) >= 2:
        second = scored_entries[1]
        gap = score - second['score']
        if gap < gap_req:
            return None, f"2位との差が僅差 (gap={gap:.2f} < {gap_req})"
        if max_gap_req is not None and gap >= max_gap_req:
            return None, f"ギャップ上限超過 (gap={gap:.2f} >= {max_gap_req})"

    return top, f"スコア{score:.1f} オッズ{odds:.1f}"
