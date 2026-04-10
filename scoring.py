"""
keiba_ai / Step 2: スコアリングエンジン（SQLite接続版）
keiba.db の過去データを参照してスコアを算出する

使い方:
    from scoring import score_race
    results = score_race(df_shutsuba, db_path="keiba.db")
"""

import pandas as pd
import numpy as np
import sqlite3
import json
from pathlib import Path

DB_PATH = "keiba.db"

# ── 期待値ありレース判定条件 ─────────────────────────────────
EV_CONDITIONS = {
    'standout_gap':  8.0,   # ① 突出度（1位と2位のスコア差）
    'min_ev_pct':    5.0,   # ② 期待値+5%以上（推定勝率×オッズ≥1.05）
    'min_odds':      3.0,   # ③ 最低オッズ3倍（売れすぎ除外）
    'max_odds':     30.0,   # ④ 最高オッズ30倍★EV異常値修正: 30倍超はsoftmax確率が破綻
}

def calc_win_prob_from_score(score_rank1: float, score_rank2: float,
                              all_scores: list) -> float:
    """
    スコアからソフトマックスで推定勝率を算出（1位馬の勝率）
    温度パラメータ12はスコア差10ptで約2.3倍の勝率差
    """
    arr = np.array(all_scores, dtype=float)
    exp_s = np.exp((arr - arr.mean()) / 12)
    probs = exp_s / exp_s.sum()
    return float(probs[0])  # 1位（スコア最高）の推定勝率


def is_ev_race(standout_gap: float, honmei_odds,
               honmei_win_prob: float = None) -> tuple:
    """
    期待値ありレース判定（真のEVベース）
    
    Args:
        standout_gap:     1位と2位のスコア差
        honmei_odds:      本命の単勝オッズ（実数、なければNone）
        honmei_win_prob:  本命の推定勝率（0〜1、calc_evで計算済みの値）
    
    Returns:
        (bool: 判定結果, list: 判定理由リスト)
    """
    reasons = []
    ok = True
    has_odds = (honmei_odds is not None and
                not (isinstance(honmei_odds, float) and np.isnan(honmei_odds)) and
                float(honmei_odds) > 0)

    # ① 突出度チェック（スコアの信頼性）
    th_gap = EV_CONDITIONS['standout_gap']
    if standout_gap >= th_gap:
        reasons.append({'type': 'ok',
                        'text': f'突出度 {standout_gap:.1f}pt ≥ {th_gap}pt ✅'})
    else:
        reasons.append({'type': 'ng',
                        'text': f'突出度 {standout_gap:.1f}pt < {th_gap}pt（混戦）❌'})
        ok = False

    if not has_odds:
        # オッズ未入力 → 突出度のみで暫定判定
        reasons.append({'type': 'warn',
                        'text': 'オッズ未入力 ─ 当日直前に確認 ⚠️'})
        return ok, reasons

    odds = float(honmei_odds)

    # ② 最低・最高オッズチェック（異常オッズ排除）
    if odds < EV_CONDITIONS['min_odds']:
        reasons.append({'type': 'ng',
                        'text': f'オッズ {odds:.1f}倍 < {EV_CONDITIONS["min_odds"]}倍（売れすぎ）❌'})
        ok = False
    elif odds > EV_CONDITIONS['max_odds']:
        reasons.append({'type': 'ng',
                        'text': f'オッズ {odds:.1f}倍 > {EV_CONDITIONS["max_odds"]}倍（過大・見送り）❌'})
        ok = False
    else:
        reasons.append({'type': 'ok',
                        'text': f'オッズ {odds:.1f}倍 ✅'})

    # ③ 真の期待値チェック（推定勝率×オッズ）
    if honmei_win_prob is not None:
        ev_pct = (honmei_win_prob * odds - 1) * 100
        th_ev = EV_CONDITIONS['min_ev_pct']
        if ev_pct >= th_ev:
            reasons.append({'type': 'ok',
                            'text': f'期待値 +{ev_pct:.1f}% ≥ +{th_ev}% ✅'
                                    f'（推定勝率{honmei_win_prob*100:.1f}% × {odds:.1f}倍）'})
        else:
            reasons.append({'type': 'ng',
                            'text': f'期待値 {ev_pct:+.1f}% < +{th_ev}%（割に合わない）❌'
                                    f'（推定勝率{honmei_win_prob*100:.1f}% × {odds:.1f}倍）'})
            ok = False
    else:
        # 推定勝率未計算時 ─ オッズのみで簡易判定
        reasons.append({'type': 'warn',
                        'text': '推定勝率未計算（スコアリング後に再判定）'})

    return ok, reasons

# ── 重み係数 ──────────────────────────────────────────────
WEIGHTS = {
    "past_performance": 0.20,  # 過去成績・タイム
    "course_fitness":   0.17,  # コース・距離・馬場適性
    "jockey_trainer":   0.12,  # 騎手・調教師（市場織込済→微増）
    "rotation":         0.07,  # ローテーション（予測力弱→微減）
    "training":         0.20,  # 調教実データ（市場非織込★）
    "sire":             0.10,  # 父血統（コスパ高→増）
    "dam_sire":         0.06,  # 母父血統
    "gate_style":       0.08,  # 枠順・脚質（市場非織込★）
}

# ── 新馬・未勝利専用ウェイト ──────────────────────────────
WEIGHTS_MAIDEN = {
    "past_performance": 0.05,
    "course_fitness":   0.09,
    "jockey_trainer":   0.19,
    "rotation":         0.05,
    "training":         0.33,
    "sire":             0.16,
    "dam_sire":         0.07,
    "gate_style":       0.06,  # 新馬でも枠順は多少効く
}

# ── 枠順・脚質バイアステーブル（DBから集計済み） ─────────
# 形式: {(venue, surface, distance): {'gate': {...}, 'style': {...}}}
# gate  キー: 馬番グループ (1=1-2番, 2=3-4番, 3=5-7番, 4=8-11番, 5=12-14番, 6=15+番)
# style キー: '逃げ'|'先行'|'中団'|'差追'  値: 平均との差分(%)
_GATE_STYLE_BIAS: dict = {}  # 起動時にJSONから読み込む

def _load_gate_style_bias(json_path: str = 'gate_style_bias.json') -> None:
    """gate_style_bias.jsonを読み込んでキャッシュに格納"""
    import json
    from pathlib import Path
    p = Path(json_path)
    if not p.exists():
        return
    with open(p, encoding='utf-8') as f:
        raw = json.load(f)
    for key, data in raw.items():
        venue   = data['venue']
        surface = data['surface']
        dist    = int(data['distance'])
        avg_g   = data.get('avg_win_pct', 0)
        avg_s   = data.get('avg_style_wr', 0)

        # 枠順グループの差分を格納
        gate_diff = {}
        for lbl, v in data.get('gate', {}).items():
            gate_diff[lbl] = v.get('diff', 0)

        # 脚質の差分を格納
        style_diff = {}
        for s, v in data.get('style', {}).items():
            style_diff[s] = v.get('diff', 0)

        _GATE_STYLE_BIAS[(venue, surface, dist)] = {
            'gate':  gate_diff,
            'style': style_diff,
            'avg_g': avg_g,
            'avg_s': avg_s,
        }

# モジュール読み込み時に自動ロード
_load_gate_style_bias()


def _horse_num_to_gate_label(horse_num: int) -> str:
    """馬番 → 枠グループラベル"""
    if horse_num <= 2:   return '内(1-2)'
    if horse_num <= 4:   return '中内(3-4)'
    if horse_num <= 7:   return '中(5-7)'
    if horse_num <= 11:  return '中外(8-11)'
    if horse_num <= 14:  return '外(12-14)'
    return '大外(15+)'


# _infer_running_style用キャッシュ（プリフェッチで埋める）
_running_style_cache: dict = {}

def _infer_running_style(horse_name: str, race_date: str,
                          surface: str, distance: int, conn) -> str:
    """
    直近3走のpos4（4角通過順位）から脚質を推定
    戻り値: '逃げ'|'先行'|'中団'|'差追'|None
    キャッシュ（_running_style_cache）があれば即返却。
    """
    cache_key = (horse_name, race_date[:7])  # 月単位キャッシュ
    if cache_key in _running_style_cache:
        return _running_style_cache[cache_key]

    rows = conn.execute('''
        SELECT pos4, num_horses FROM results
        WHERE horse_name=? AND date<? AND pos4>0 AND num_horses>0
          AND finish<90
        ORDER BY date DESC LIMIT 3
    ''', (horse_name, race_date)).fetchall()

    if not rows:
        result = None
    else:
        ratios = [r['pos4'] / r['num_horses'] for r in rows]
        avg_ratio = sum(ratios) / len(ratios)
        if avg_ratio <= 0.20:  result = '逃げ'
        elif avg_ratio <= 0.45: result = '先行'
        elif avg_ratio <= 0.70: result = '中団'
        else:                   result = '差追'

    _running_style_cache[cache_key] = result
    return result



# ══════════════════════════════════════════════════════════
# 展開予測補正（逃げ馬頭数×コースバイアスの動的補正）
# ══════════════════════════════════════════════════════════

# 逃げ馬頭数別の脚質補正係数
# 根拠: DB分析（2021-2025年・1000-2400m・8頭以上）
# 逃げ1頭: 逃げ41.8% 先行28.3% 差し15.4% 追込6.0% （基準）
# 逃げ2頭: ペース上がりやすい → 差し・追込を底上げ
# 逃げ3頭+: さらにペース激化の可能性 → 差し・追込をさらに底上げ
# ただし逃げ2頭以上のサンプルは少ないため補正は控えめに
_PACE_STYLE_MULT = {
    # {逃げ頭数: {脚質: 係数}}
    # 係数1.0=変化なし、1.2=+20%加点、0.8=-20%減点
    0: {'逃げ': 0.80, '先行': 1.10, '中団': 1.00, '差追': 0.90},  # 逃げ不在 = スロー = 先行天国
    1: {'逃げ': 1.00, '先行': 1.00, '中団': 1.00, '差追': 1.00},  # 標準（変化なし）
    2: {'逃げ': 0.95, '先行': 0.90, '中団': 1.10, '差追': 1.15},  # ハイペース気味 = 差し有利
    3: {'逃げ': 0.85, '先行': 0.80, '中団': 1.15, '差追': 1.25},  # ハイペース = 差し有利
}
_PACE_STYLE_MULT[4] = _PACE_STYLE_MULT[3]  # 4頭以上は3頭と同じ

# PCI実値による補正係数（race_paceテーブルから取得した前走PCI平均）
# 根拠: race_pace × results JOINで確認（2019-2025年・7年分）
# PCI<48（ハイペース）: 逃先30% vs 差追12%（差18pt）→ 逃先優位が最大
# PCI48-52（普通）:     逃先28% vs 差追12%（差16pt）→ 標準
# PCI>52（スロー）:     逃先26% vs 差追13%（差13pt）→ 差し接近
_PCI_STYLE_MULT = {
    'high':   {'逃げ': 1.10, '先行': 1.05, '中団': 0.95, '差追': 0.85},  # ハイペース(PCI<48)
    'normal': {'逃げ': 1.00, '先行': 1.00, '中団': 1.00, '差追': 1.00},  # 普通(48-52)
    'slow':   {'逃げ': 0.90, '先行': 0.95, '中団': 1.05, '差追': 1.10},  # スロー(PCI>52)
}


def calc_pace_context(race_styles: list, recent_pci: float = None) -> dict:
    """
    同レース出走馬の脚質リストから展開コンテキストを計算する。

    Args:
        race_styles: 各馬の推定脚質リスト
        recent_pci:  直近同コースの平均PCI（race_paceテーブルから取得）
                     指定時はPCI実値ベースの補正係数を使用
    Returns:
        dict: {'nige_count': int, 'mult': dict, 'pci_mode': str}
    """
    nige_count = sum(1 for s in race_styles if s == '逃げ')

    if recent_pci is not None:
        # PCI実値ベースの補正（より精度が高い）
        if   recent_pci < 48: pci_mode = 'high'
        elif recent_pci > 52: pci_mode = 'slow'
        else:                 pci_mode = 'normal'
        mult = _PCI_STYLE_MULT[pci_mode]
    else:
        # フォールバック: 逃げ頭数推定（従来方式）
        nige_key = min(nige_count, 3)
        mult     = _PACE_STYLE_MULT.get(nige_key, _PACE_STYLE_MULT[1])
        pci_mode = 'estimated'

    return {'nige_count': nige_count, 'mult': mult, 'pci_mode': pci_mode}

def score_gate_style(horse_name: str, horse_num: int,
                     race_date: str, venue: str,
                     surface: str, distance: int, conn,
                     pace_mult: dict = None) -> dict:
    """
    枠順・脚質の有利不利をスコアに変換（0〜100）
    ベース50、有利なら加点、不利なら減点

    Args:
        horse_num:  馬番（0の場合はスコアなし）
        venue:      開催場所（例: '中京'）
        pace_mult:  展開補正係数 calc_pace_context() の返すmult辞書
                    Noneなら展開補正なし（静的バイアスのみ）
    """
    bias = _GATE_STYLE_BIAS.get((venue, surface, distance))
    if not bias:
        # バイアスなし: 脚質推定だけ返す（style_diff=0で影響なし）
        style_fb = _infer_running_style(horse_name, race_date, surface, distance, conn)
        pm_fb = pace_mult.get(style_fb, 1.0) if pace_mult and style_fb else 1.0
        return {'score': 50.0, 'gate_diff': 0.0, 'style_diff': 0.0,
                'style': style_fb, 'pace_mult': pm_fb, 'nige_count': 0}

    score   = 50.0
    g_diff  = 0.0
    s_diff  = 0.0

    # ── 枠順補正 ──────────────────────────────────────────
    if horse_num and horse_num > 0:
        lbl    = _horse_num_to_gate_label(horse_num)
        g_diff = bias['gate'].get(lbl, 0.0)
        score += g_diff * 2.5

    # ── 脚質補正（静的コースバイアス）──────────────────────
    # gate_style_bias の style キー: '逃げ'|'先行'|'中団'|'差追'
    style = _infer_running_style(horse_name, race_date, surface, distance, conn)
    if style:
        s_diff = bias['style'].get(style, 0.0)
        # ── 展開補正（動的ペース予測）────────────────────
        # _infer_running_style は '逃げ'|'先行'|'中団'|'差追' を返す
        # pace_mult のキーは '逃げ'|'先行'|'中団'|'差追' に合わせる
        if pace_mult:
            mult = pace_mult.get(style, 1.0)
        else:
            mult = 1.0
        score += s_diff * 2.0 * mult

    score = max(0.0, min(100.0, score))

    return {
        'score':      round(score, 1),
        'gate_diff':  round(g_diff, 1),
        'style_diff': round(s_diff, 1),
        'style':      style,
        'pace_mult':  round(pace_mult.get(style, 1.0) if pace_mult and style else 1.0, 2),
    }

MAIDEN_GRADES = {'新馬', '未勝利', '未出走'}

def get_weights(grade: str) -> dict:
    """レース種別に応じたウェイト辞書を返す"""
    if any(g in str(grade) for g in MAIDEN_GRADES):
        return WEIGHTS_MAIDEN
    return WEIGHTS

# ── DB接続ユーティリティ ───────────────────────────────────
def get_conn(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ══════════════════════════════════════════════════════════
# 1. 過去成績スコア（DBから過去走を取得）
# ══════════════════════════════════════════════════════════

# ── バックテスト高速化: 過去走・コース適性の一括プリフェッチ用キャッシュ ─
_past_runs_cache:   dict = {}   # (horse_name, ym) → list of rows
_course_runs_cache: dict = {}   # (horse_name, ym) → list of rows

# ── スコア計算用キャッシュ（バックテスト高速化）────────────
_avg_time_cache:  dict = {}
_last3f_cache:    dict = {}

def _get_avg_time(conn, surface, distance, cond):
    """平均走破タイムをキャッシュ付きで取得"""
    key = (surface, distance, cond)
    if key not in _avg_time_cache:
        r = conn.execute('''
            SELECT AVG(time_sec) as avg_t, MIN(time_sec) as best_t
            FROM results
            WHERE surface=? AND distance=? AND track_cond=?
              AND time_sec IS NOT NULL AND finish=1
        ''', (surface, distance, cond)).fetchone()
        _avg_time_cache[key] = (r['avg_t'], r['best_t']) if r else (None, None)
    return _avg_time_cache[key]

def _grade_from_race_name(race_name):
    """race_nameからクラスを推定"""
    rn = str(race_name or '')
    if '新馬' in rn: return '新馬'
    if '未勝利' in rn: return '未勝利'
    if '1勝' in rn or '１勝' in rn: return '1勝'
    if '2勝' in rn or '２勝' in rn: return '2勝'
    if '3勝' in rn or '３勝' in rn or 'オープン' in rn: return '3勝'
    return None


_last3f_class_cache: dict = {}

def _get_avg_last3f_by_class(conn, venue, distance, surface, date, grade=None):
    """上がり3F平均をクラス別で取得（フォールバック付き）"""
    key = (venue, distance, surface, date[:7], grade)
    if key in _last3f_class_cache:
        return _last3f_class_cache[key]

    if grade:
        grade_patterns = {
            '新馬': '%新馬%', '未勝利': '%未勝利%',
            '1勝': '%1勝%', '2勝': '%2勝%', '3勝': '%3勝%',
        }
        pattern = grade_patterns.get(grade)
        if pattern:
            r = conn.execute('''
                SELECT AVG(last3f), COUNT(*) FROM results
                WHERE venue=? AND distance=? AND surface=?
                  AND date BETWEEN date(?, '-1 year') AND ?
                  AND race_name LIKE ? AND last3f > 0
            ''', (venue, distance, surface, date, date, pattern)).fetchone()
            if r and r[1] and r[1] >= 20:
                _last3f_class_cache[key] = r[0]
                return r[0]

    # フォールバック: 既存の全クラス混合
    val = _get_avg_last3f(conn, venue, distance, surface, date)
    _last3f_class_cache[key] = val
    return val


def _get_avg_last3f(conn, venue, distance, surface, date):
    """上がり3F平均をキャッシュ付きで取得（月単位キャッシュ）
    prefetch_score_caches() 実行済みの場合は '__all__' キーで全期間平均を返す（高速）
    未実行の場合は従来通りDBクエリを実行する
    """
    # 高速パス: 全件プリフェッチ済みの場合
    all_key = ('__all__', venue, distance, surface)
    if all_key in _last3f_cache:
        return _last3f_cache[all_key]
    # 通常パス: 月単位DBクエリ
    key = (venue, distance, surface, date[:7])
    if key not in _last3f_cache:
        r = conn.execute('''
            SELECT AVG(last3f) FROM results
            WHERE venue=? AND distance=? AND surface=?
              AND date BETWEEN date(?, '-1 year') AND ?
        ''', (venue, distance, surface, date, date)).fetchone()
        _last3f_cache[key] = r[0] if r else None
    return _last3f_cache[key]

def clear_score_cache():
    """キャッシュをクリア（新しいDBを読み込む際に使用）"""
    _avg_time_cache.clear()
    _last3f_cache.clear()
    _last3f_class_cache.clear()
    _surface_switch_cache.clear()


def score_past_performance(horse_name: str, race_date: str,
                           surface: str, distance: int,
                           conn) -> dict:
    """
    過去3走の着順・タイム偏差・上がり3Fから算出
    同コース・同距離を優先的に参照
    ※ キャッシュにより2回目以降は高速動作
    """
    # プリフェッチキャッシュを参照（高速化）
    ym = race_date[:7]
    cache_key = (horse_name, ym)
    if cache_key in _past_runs_cache:
        rows = _past_runs_cache[cache_key]
    else:
        rows = conn.execute("""
            SELECT finish, time_sec, last3f, surface, distance,
                   track_cond, venue, num_horses, date, margin, race_name
            FROM results
            WHERE TRIM(horse_name) = ?
              AND date < ?
              AND finish IS NOT NULL
              AND finish < 90
            ORDER BY date DESC
            LIMIT 5
        """, (horse_name, race_date)).fetchall()
        # sqlite3.Row → dict に変換して .get() が使えるようにする
        rows = [dict(r) for r in rows]
        _past_runs_cache[cache_key] = rows

    if not rows:
        return {"score": 50.0, "n": 0, "detail": "データなし"}

    scores = []
    for i, row in enumerate(rows[:3]):
        w = [1.0, 0.8, 0.6][i]

        f = row['finish']
        if f == 1:   fs = 100
        elif f == 2: fs = 85
        elif f == 3: fs = 72
        elif f <= 5: fs = 58
        else:        fs = max(10, 55 - (f - 5) * 4)

        # ── 着差補正（margin）────────────────────────────
        # 2着以下で「惜しい負け」→ 加点 / 「大差負け」→ 追加減点
        # margin: 1着馬との差（秒）。1着馬自身はmargin=0なのでスキップ
        margin = row.get('margin')
        if margin is not None and f >= 2:
            try:
                m = float(margin)
                if m < 0:  # TARGETの仕様で2着=負の着差の場合
                    m = abs(m)
                if   m <= 0.1:  fs = min(100, fs + 8)   # ハナ差〜クビ差: 大幅加点
                elif m <= 0.3:  fs = min(100, fs + 5)   # クビ〜1/2馬身
                elif m <= 0.5:  fs = min(100, fs + 3)   # 1/2〜1馬身
                elif m >= 3.0:  fs = max(0,   fs - 8)   # 3馬身以上: 大差負け
                elif m >= 1.5:  fs = max(0,   fs - 4)   # 1.5〜3馬身
            except (TypeError, ValueError):
                pass

        # タイム偏差スコア（キャッシュ使用）
        ts = 60.0
        if row['time_sec'] and row['surface'] and row['distance']:
            avg_t, _ = _get_avg_time(conn, row['surface'], row['distance'], row['track_cond'])
            if avg_t:
                diff = row['time_sec'] - avg_t
                ts = max(0, min(100, 75 - diff * 8))

        # 上がり3Fスコア（クラス別平均、キャッシュ使用）
        ls = 60.0
        if row['last3f']:
            row_grade = _grade_from_race_name(row.get('race_name', ''))
            avg_last = _get_avg_last3f_by_class(conn, row['venue'], row['distance'],
                                                 row['surface'], row['date'], row_grade)
            if avg_last:
                diff = avg_last - row['last3f']
                ls = max(0, min(100, 60 + diff * 6))

        scores.append((fs * 0.5 + ts * 0.3 + ls * 0.2) * w)

    total_w = sum([1.0, 0.8, 0.6][:len(scores)])
    final = sum(scores) / total_w if total_w > 0 else 50.0

    return {
        "score": round(final, 1),
        "n": len(rows),
        "detail": f"直近{len(rows)}走参照"
    }


# ══════════════════════════════════════════════════════════
# 2. コース・距離・馬場 適性スコア
# ══════════════════════════════════════════════════════════
_course_fitness_cache: dict = {}  # (horse, date, venue, surf, dist, cond) → dict
_wet_perf_cache: dict = {}       # (horse, ym, 'wet') → wet_rate (0-1 or -1)

def score_course_fitness(horse_name: str, race_date: str,
                         surface: str, distance: int,
                         track_cond: str, conn,
                         venue: str = '') -> dict:
    """
    コース・距離適性スコア（0〜100）
    結果キャッシュ付き（2回目以降は0.001ms/回）
    """
    _cf_ck = (horse_name, race_date, venue, surface, distance, track_cond)
    if _cf_ck in _course_fitness_cache:
        return _course_fitness_cache[_cf_ck]

    def calc_score_from_rows(rows):
        if not rows: return None, 0
        wins = sum(1 for r in rows if r['finish'] == 1)
        top3 = sum(1 for r in rows if r['finish'] <= 3)
        n    = len(rows)
        sc   = wins / n * 60 + top3 / n * 40
        return min(100, sc * 1.5 + 30), n

    # ── キャッシュ参照（全データはdictで格納済み） ───────────
    ym = race_date[:7]
    cf_key  = (horse_name, ym, surface, distance // 400)
    # venue付きキャッシュキー（_prefetch_past_runsでvenueフィールド付きdictが格納済み）
    vcf_key = (horse_name, ym, surface, distance // 400, venue)

    # ── プリフェッチ済みデータから取得 ────────────────────
    if cf_key in _course_runs_cache:
        cached = _course_runs_cache[cf_key]   # list of dict
        # ±200m
        rows_200 = [r for r in cached if abs(r['distance'] - distance) <= 200]
        # 同会場±200m
        rows_v   = [r for r in rows_200 if r.get('venue','') == venue or r.get('v','') == venue]
        # ±400m
        rows_400 = [r for r in cached if abs(r['distance'] - distance) <= 400]
    else:
        # キャッシュなし→直接DB取得
        raw = conn.execute("""
            SELECT finish, distance, venue as v
            FROM results
            WHERE horse_name=? AND date<? AND surface=?
              AND distance BETWEEN ? AND ? AND finish<90
        """, (horse_name, race_date, surface,
              distance - 800, distance + 800)).fetchall()
        cached = [{'finish':r['finish'],'distance':r['distance'],
                   'venue':r['v'],'v':r['v']} for r in raw]
        rows_200 = [r for r in cached if abs(r['distance']-distance) <= 200]
        rows_v   = [r for r in rows_200 if r.get('v','') == venue]
        rows_400 = [r for r in cached if abs(r['distance']-distance) <= 400]

    score_200, n_200 = calc_score_from_rows(rows_200)
    score_v,   n_v   = calc_score_from_rows(rows_v)

    # ── ① 同会場・同距離（±200m） ────────────────────────
    # → rows_v / score_v / n_v 使用（上で計算済み）

    # ── ② 同距離±400m（会場問わず） ──────────────────────
    if n_200 < 3:
        score_400, n_400 = calc_score_from_rows(rows_400)
    else:
        score_400, n_400 = None, 0

    # ── ③ 同芝ダ全体 ──────────────────────────────────────
    if n_200 < 3 and n_400 < 3:
        rows_all = conn.execute("""
            SELECT finish FROM results
            WHERE horse_name=? AND date<? AND surface=? AND finish<90
        """, (horse_name, race_date, surface)).fetchall()
        score_all, n_all = calc_score_from_rows(rows_all)
    else:
        score_all, n_all = None, 0

    # ── スコア決定（優先度順） ────────────────────────────
    # 同会場成績が3走以上あれば最優先（重みを70%に）
    if score_v is not None and n_v >= 3:
        # 同会場（高信頼）+ 全距離（補完）
        score = score_v * 0.7 + (score_200 or score_v) * 0.3
        n = n_v
    elif score_200 is not None and n_200 >= 3:
        score = score_200
        n = n_200
    elif score_400 is not None and n_400 >= 2:
        # データ少: 中間値に引き寄せる
        score = score_400 * 0.7 + 50.0 * 0.3
        n = n_400
    elif score_all is not None and n_all >= 2:
        score = score_all * 0.5 + 50.0 * 0.5  # 信頼度低：さらに中間値寄り
        n = n_all
    else:
        score = 50.0
        n = 0

    # ── 道悪適性 ──────────────────────────────────────────
    if track_cond in ['稍重', '重', '不良']:
        wet_rows = conn.execute("""
            SELECT finish FROM results
            WHERE TRIM(horse_name) = ?
              AND date < ?
              AND track_cond IN ('稍重', '重', '不良')
              AND finish < 90
        """, (horse_name, race_date)).fetchall()
        if wet_rows:
            wet_top3 = sum(1 for r in wet_rows if r['finish'] <= 3)
            wet_rate = wet_top3 / len(wet_rows)
            score = score * 0.6 + wet_rate * 100 * 0.4

    _r = {"score": round(score, 1), "n": n}; _course_fitness_cache[_cf_ck] = _r; return _r


# ══════════════════════════════════════════════════════════
# 3. 騎手・調教師スコア（コンビ補正＋エース補正込み）
# ══════════════════════════════════════════════════════════
# キャッシュ辞書（バックテスト高速化）
_jockey_cache:  dict = {}
_trainer_cache: dict = {}
_combo_cache:   dict = {}
_ace_cache:     dict = {}

def score_jockey_trainer(jockey: str, trainer: str, race_date: str,
                         surface: str, distance: int, conn,
                         horse_name: str = '', grade: str = '') -> dict:
    """
    DBから騎手・調教師の当該コース別勝率を算出。
    さらに以下の2補正を加算する:
      ① ホットライン補正: コンビ勝率 > 騎手単独勝率 なら加点
      ② エース補正:       直近1年の厩舎内で最多勝ち数の馬なら加点

    未勝利・新馬の場合は**クラス限定勝率**を使用（全体勝率と別キャッシュ）。
    未勝利で強い騎手・調教師を正しく評価するため。
    """

    is_maiden = any(g in str(grade) for g in MAIDEN_GRADES)

    # キャッシュキー: 半期単位（同じ騎手/厩舎は1ヶ月内で変わらない）
    yh = race_date[:4] + ('H1' if race_date[5:7] <= '06' else 'H2')
    # 未勝利は別キャッシュキーにする
    grade_tag = 'M' if is_maiden else 'A'

    # ── 騎手スコア（キャッシュ付き） ────────────────────────
    j_key = (jockey, surface, distance // 400, yh, grade_tag)
    if j_key in _jockey_cache:
        j_score, j_wr = _jockey_cache[j_key]
    else:
        if is_maiden:
            # 未勝利・新馬限定の勝率（距離帯フィルタは維持）
            j_rows = conn.execute("""
                SELECT finish FROM results
                WHERE jockey = ?
                  AND date BETWEEN date(?, '-2 year') AND ?
                  AND surface = ?
                  AND distance BETWEEN ? AND ?
                  AND finish < 90
                  AND (race_name LIKE '%未勝利%' OR race_name LIKE '%新馬%')
            """, (jockey, race_date, race_date, surface,
                  distance - 400, distance + 400)).fetchall()
        else:
            j_rows = conn.execute("""
                SELECT finish FROM results
                WHERE jockey = ?
                  AND date BETWEEN date(?, '-2 year') AND ?
                  AND surface = ?
                  AND distance BETWEEN ? AND ?
                  AND finish < 90
            """, (jockey, race_date, race_date, surface,
                  distance - 400, distance + 400)).fetchall()
        if len(j_rows) >= 10:
            j_wins  = sum(1 for r in j_rows if r['finish'] == 1)
            j_top3  = sum(1 for r in j_rows if r['finish'] <= 3)
            j_score = min(100, j_wins / len(j_rows) * 300 + j_top3 / len(j_rows) * 50)
            j_wr    = j_wins / len(j_rows)
        elif is_maiden and len(j_rows) >= 5:
            # 未勝利はサンプル少なめでも判定（閾値5に緩和）
            j_wins  = sum(1 for r in j_rows if r['finish'] == 1)
            j_top3  = sum(1 for r in j_rows if r['finish'] <= 3)
            j_score = min(100, j_wins / len(j_rows) * 300 + j_top3 / len(j_rows) * 50)
            j_wr    = j_wins / len(j_rows)
        else:
            j_score = 60.0; j_wr = 0.10
        _jockey_cache[j_key] = (j_score, j_wr)

    # ── 調教師スコア（キャッシュ付き） ──────────────────────
    t_key = (trainer, yh, grade_tag)
    if t_key in _trainer_cache:
        t_score = _trainer_cache[t_key]
    else:
        if is_maiden:
            # 未勝利・新馬限定の調教師勝率
            t_rows = conn.execute("""
                SELECT finish FROM results
                WHERE trainer = ?
                  AND date BETWEEN date(?, '-1 year') AND ?
                  AND finish < 90
                  AND (race_name LIKE '%未勝利%' OR race_name LIKE '%新馬%')
            """, (trainer, race_date, race_date)).fetchall()
        else:
            t_rows = conn.execute("""
                SELECT finish FROM results
                WHERE trainer = ?
                  AND date BETWEEN date(?, '-1 year') AND ?
                  AND finish < 90
            """, (trainer, race_date, race_date)).fetchall()
        if len(t_rows) >= 10:
            t_wins  = sum(1 for r in t_rows if r['finish'] == 1)
            t_score = min(100, t_wins / len(t_rows) * 400)
        elif is_maiden and len(t_rows) >= 5:
            t_wins  = sum(1 for r in t_rows if r['finish'] == 1)
            t_score = min(100, t_wins / len(t_rows) * 400)
        else:
            t_score = 60.0
        _trainer_cache[t_key] = t_score

    # ── ① ホットライン補正（キャッシュ付き） ────────────────
    c_key = (jockey, trainer, yh)
    if c_key in _combo_cache:
        combo_bonus = _combo_cache[c_key]
    else:
        combo_bonus = 0.0
        c_rows = conn.execute("""
            SELECT finish FROM results
            WHERE jockey = ? AND trainer = ?
              AND date BETWEEN date(?, '-2 year') AND ?
              AND finish < 90
        """, (jockey, trainer, race_date, race_date)).fetchall()
        if len(c_rows) >= 10:
            c_wins = sum(1 for r in c_rows if r['finish'] == 1)
            c_wr   = c_wins / len(c_rows)
            combo_bonus = max(-5.0, min(5.0, (c_wr - j_wr) * 60))
        _combo_cache[c_key] = combo_bonus

    # ── ② エース騎手補正（キャッシュ付き） ──────────────────
    a_key = (jockey, trainer, yh)
    if a_key in _ace_cache:
        ace_bonus, ace_pct = _ace_cache[a_key]
    else:
        ace_bonus = 0.0; ace_pct = 0.0
        ace_rows = conn.execute("""
            SELECT jockey,
                   SUM(CASE WHEN finish=1 THEN 1 ELSE 0 END) as wins
            FROM results
            WHERE trainer = ?
              AND date BETWEEN date(?, '-2 year') AND ?
              AND finish < 90
            GROUP BY jockey
        """, (trainer, race_date, race_date)).fetchall()
        if ace_rows:
            total_tw = sum(r['wins'] for r in ace_rows)
            if total_tw >= 5:
                this_wins = next((r['wins'] for r in ace_rows if r['jockey'] == jockey), 0)
                ace_pct   = this_wins / total_tw * 100
                if   ace_pct >= 40: ace_bonus = 3.0
                elif ace_pct >= 25: ace_bonus = 2.0
                elif ace_pct >= 15: ace_bonus = 1.0
                elif ace_pct >=  8: ace_bonus = 0.5
        _ace_cache[a_key] = (ace_bonus, ace_pct)

    # ── 合算 ───────────────────────────────────────────────
    base  = j_score * 0.55 + t_score * 0.30
    final = base + combo_bonus + ace_bonus
    final = max(0.0, min(100.0, final))

    return {
        "score":       round(final, 1),
        "j_score":     round(j_score, 1),
        "t_score":     round(t_score, 1),
        "combo_bonus": round(combo_bonus, 1),
        "ace_bonus":   round(ace_bonus, 1),
        "ace_pct":     round(ace_pct, 1),
    }


# ══════════════════════════════════════════════════════════
# 3.5. 前走内容スコア（レース後コメントの代替）
# ══════════════════════════════════════════════════════════
_prev_content_cache: dict = {}

def score_prev_content(horse_name: str, race_date: str, conn) -> dict:
    """
    前走のレース内容を数値化して「次走での上昇/下降」を推定。
    レース後コメントの代替として、以下の指標を使用:

    1. 上がり3F順位 vs 着順の乖離 → 「脚を余した」パターン検出
    2. 4角位置 vs 着順の乖離 → 「不利があった」パターン検出
    3. 着差(margin)の小ささ → 「惜しい負け」パターン検出
    4. 前走人気 vs 着順 → 「過小評価だった」パターン検出
    """
    cache_key = (horse_name, race_date[:7])
    if cache_key in _prev_content_cache:
        return _prev_content_cache[cache_key]

    rows = conn.execute("""
        SELECT finish, last3f, pos4, margin, num_horses, popularity,
               surface, distance, venue, date
        FROM results
        WHERE horse_name = ?
          AND date < ?
          AND finish IS NOT NULL AND finish < 90
        ORDER BY date DESC
        LIMIT 1
    """, (horse_name, race_date)).fetchall()

    if not rows:
        result = {"score": 50.0, "detail": "前走なし"}
        _prev_content_cache[cache_key] = result
        return result

    r = dict(rows[0])
    score = 50.0  # ベース
    fin = r['finish']
    heads = r['num_horses'] or 16

    # 1. 上がり3F順位 vs 着順（脚を余したか）
    if r['last3f'] and r['last3f'] > 0:
        # 同レースの上がり3F順位を取得
        l3f_rank_row = conn.execute("""
            SELECT COUNT(*) + 1 as rank FROM results
            WHERE date=? AND venue=? AND race_num=(
                SELECT race_num FROM results WHERE horse_name=? AND date=? AND venue=? LIMIT 1
            ) AND last3f < ? AND last3f > 0 AND finish < 90
        """, (r['date'], r['venue'], horse_name, r['date'], r['venue'], r['last3f'])).fetchone()
        l3f_rank = l3f_rank_row['rank'] if l3f_rank_row else fin

        # 上がり3F順位が着順より良い → 脚を余した（次走上昇要因）
        gap = fin - l3f_rank
        if gap >= 5:    score += 15  # 大幅に脚を余した
        elif gap >= 3:  score += 10
        elif gap >= 1:  score += 5
        elif gap <= -3: score -= 5   # 上がりが遅いのに好着順 → 展開利

    # 2. 4角位置 vs 着順（不利・位置取りロス）
    if r['pos4'] and r['pos4'] > 0:
        # 4角で後方にいたのに好着順 → 実力あり
        pos4_ratio = r['pos4'] / heads
        if fin <= 3 and pos4_ratio > 0.6:
            score += 10  # 後方から3着内 → 強い競馬
        elif fin <= 5 and pos4_ratio > 0.7:
            score += 5
        # 4角で前にいたのに大敗 → 実力不足 or 距離不適
        if fin > heads * 0.6 and pos4_ratio < 0.3:
            score -= 8

    # 3. 着差（惜しい負け）
    if r['margin'] is not None and fin >= 2:
        m = abs(float(r['margin']))
        if fin <= 5:
            if m <= 0.2:   score += 10  # 僅差の負け（3着内で0.2秒差以内）
            elif m <= 0.5: score += 5
        elif fin <= 8 and m <= 0.5:
            score += 3  # 掲示板付近で僅差

    # 4. 前走人気 vs 着順（過小評価検出）
    if r['popularity'] and r['popularity'] > 0:
        pop = r['popularity']
        if fin < pop - 3:     score += 8   # 人気より大幅に好走
        elif fin < pop - 1:   score += 4
        elif fin > pop + 5:   score -= 5   # 人気を大幅に裏切った

    score = max(0.0, min(100.0, score))
    result = {"score": round(score, 1), "detail": f"前走{int(fin)}着"}
    _prev_content_cache[cache_key] = result
    return result


# ══════════════════════════════════════════════════════════
# 4. ローテーションスコア
# ══════════════════════════════════════════════════════════
def score_rotation(interval_weeks, prev_finish, prev_distance,
                   distance: int, grade: str, prev_grade: str = '',
                   horse_weight: int = 0, surface: str = '',
                   weight_change: float = None) -> dict:
    """前走間隔・距離変化・クラス昇降・馬体重からスコア算出

    馬体重ペナルティ（7年データ検証済み）:
      420kg未満  : -15点（G1勝率1.5%）
      420〜440kg : -10点（G1 3着内12.2%）
      440〜460kg :  -5点（G1 3着内14.8%）
      460kg以上  :  ±0点（標準）
    例外: 中山芝2500m（有馬記念）は軽い馬も好走するため減点なし
    """
    score = 65.0

    # 間隔スコア: 間隔単独では精度が低いため無効
    # （調教×間隔の組み合わせ分析で調教スコアが主役と判明）
    # if pd.notna(interval_weeks): ...（廃止）

    # 距離変化スコア
    if pd.notna(prev_distance) and prev_distance > 0:
        dist_diff = abs(distance - int(prev_distance))
        if dist_diff <= 200:
            score += 8
        elif dist_diff >= 800:
            score -= 12

    # 前走着順ボーナス
    if pd.notna(prev_finish):
        f = float(prev_finish)
        if f == 1:
            score += 12
        elif f <= 3:
            score += 6
        elif f >= 8:
            score -= 5

    # ── 馬体重ペナルティ（絶対値）────────────────────────
    # 中山芝2500m（有馬記念）は例外（軽い馬でも好走するコース）
    is_exception = (surface == '芝' and distance == 2500)
    weight_penalty = 0
    if horse_weight > 0 and not is_exception:
        if   horse_weight < 420: weight_penalty = -15
        elif horse_weight < 440: weight_penalty = -10
        elif horse_weight < 460: weight_penalty = -5
    score += weight_penalty

    # ── 馬体重前走比補正 ─────────────────────────────────
    # 大幅減: DB分析で3着内率17%（平均比-7pt）
    # 大幅増(+10以上): 20.8%でやや下がる（太め残り懸念）
    weight_change_adj = 0
    if weight_change is not None:
        wc = float(weight_change)
        if   wc <= -10: weight_change_adj = -8   # 大幅減 → 要注意
        elif wc <= -4:  weight_change_adj = -3   # やや減
        elif wc >= 10:  weight_change_adj = -4   # 大幅増 → 太め残り懸念
        # ±3は標準、+4〜+9は微増で問題なし
    score += weight_change_adj

    return {"score": round(min(100, max(0, score)), 1),
            "horse_weight": horse_weight,
            "weight_penalty": weight_penalty,
            "weight_change_adj": weight_change_adj}



# ══════════════════════════════════════════════════════════
# 4b. 実調教スコア（坂路CSV取込データから）
#     v2: lap1偏差値 + 加速ラップ判定
# ══════════════════════════════════════════════════════════
_training_actual_cache: dict = {}
_training_score_cache: dict = {}  # rescore_month.py用

# 調教lap1の累積統計キャッシュ {source: (mean, std)}
_training_lap1_stats: dict = {}

def prefetch_training_lap1_stats(conn, cutoff_date: str):
    """
    cutoff_date より前のtraining全データからsource別のlap1平均・標準偏差を計算。
    偏差値計算の基準値として使う。年初に1回呼ぶ。
    """
    _training_lap1_stats.clear()
    import math
    rows = conn.execute("""
        SELECT source, AVG(lap1) as mean,
               AVG(lap1*lap1) - AVG(lap1)*AVG(lap1) as var
        FROM training
        WHERE lap1 IS NOT NULL AND lap1 > 0 AND date < ?
        GROUP BY source
    """, (cutoff_date,)).fetchall()
    for r in rows:
        var = r['var'] if r['var'] and r['var'] > 0 else 0.25
        _training_lap1_stats[r['source']] = (r['mean'], math.sqrt(var))


def _lap1_to_hensachi(lap1: float, source: str) -> float:
    """lap1タイムを偏差値に変換（速い=高偏差値）"""
    if source not in _training_lap1_stats:
        # フォールバック: ハードコード統計（2019-2021実績）
        if source == 'sakuro':
            mean, std = 11.79, 0.46
        else:
            mean, std = 13.29, 0.76
    else:
        mean, std = _training_lap1_stats[source]
    if std == 0:
        std = 0.5
    return 50.0 + 10.0 * (mean - lap1) / std


def _hensachi_to_score(hensachi: float) -> float:
    """
    偏差値→スコア変換（データ検証済み）
    坂路: 偏差値65+=3着内率33-39%, 55-60=28.7%, 50-55=25.3%, <45=19%以下
    WC:   偏差値70+=26.2%, 65-70=22.2%, 60-65=21.2%, <55=15%以下
    """
    if   hensachi >= 70: return 95.0
    elif hensachi >= 65: return 88.0
    elif hensachi >= 60: return 78.0
    elif hensachi >= 55: return 68.0
    elif hensachi >= 50: return 58.0
    elif hensachi >= 45: return 50.0
    else:                return 42.0


def score_training_actual(horse_name: str, race_date: str, sc) -> dict:
    """
    レース前14日以内の最速lap1（最終1F）で調教スコアを判定。
    v2: 旧閾値ベース + 加速ラップボーナス（+5pt）

    閾値スコア（実データ検証済み）:
      坂路: <11.3=95 / <11.6=90 / <12.0=75 / <12.5=60 / else=50
      WC:   <11.0=95 / <11.3=90 / <11.5=75 / <11.7=60 / else=50

    加速ラップ（lap1 < lap2）ボーナス: +5pt
      坂路: 3着内率+3.0pt（26.1% vs 23.1%）
      WC:   3着内率+3.6pt（21.7% vs 18.1%）
    """
    cache_key = (horse_name, race_date)
    if cache_key in _training_actual_cache:
        return _training_actual_cache[cache_key]

    rows = sc.execute("""
        SELECT lap1, lap2, source
        FROM training
        WHERE horse_name = ?
          AND date BETWEEN date(?, '-14 days') AND date(?, '-1 days')
          AND lap1 IS NOT NULL AND lap1 > 0
        ORDER BY lap1 ASC
        LIMIT 1
    """, (horse_name, race_date, race_date)).fetchall()

    if rows:
        lap1 = rows[0]['lap1']
        lap2 = rows[0]['lap2']
        src  = rows[0]['source'] if rows[0]['source'] else 'sakuro'

        if src == 'woodc':
            if   lap1 < 11.0: score = 95.0
            elif lap1 < 11.3: score = 90.0
            elif lap1 < 11.5: score = 75.0
            elif lap1 < 11.7: score = 60.0
            else:             score = 50.0
            has_good_train = (lap1 < 11.5)
        else:
            if   lap1 < 11.3: score = 95.0
            elif lap1 < 11.6: score = 90.0
            elif lap1 < 12.0: score = 75.0
            elif lap1 < 12.5: score = 60.0
            else:             score = 50.0
            has_good_train = (lap1 < 12.0)

        # 加速ラップ判定（lap1 < lap2 = 末脚が速い = 仕上がり良好）
        # スコアには加算しない（+5ptは検証済みで悪化: 2024年ROI 133→104%）
        accel = False
        if lap2 is not None and lap2 > 0 and lap1 < lap2:
            accel = True
    else:
        score = 48.0
        has_good_train = False
        accel = False

    result = {
        'score': score,
        'has_good_train': has_good_train,
        'accel_lap': accel,
    }
    _training_actual_cache[cache_key] = result
    return result


def score_training(horse_name: str, race_date: str, conn) -> dict:
    """
    調教実データ（training テーブル）からスコアを算出。
    レース前7日以内の坂路調教を使用。
    Lap1・Lap2 両方≤12.4 → 高評価
    """
    key = (horse_name, race_date)
    if key in _training_score_cache:
        return _training_score_cache[key]

    rows = conn.execute("""
        SELECT lap1, lap2, time1
        FROM training
        WHERE horse_name = ?
          AND date BETWEEN date(?, '-7 days') AND date(?, '-1 days')
        ORDER BY lap1 ASC, lap2 ASC
        LIMIT 3
    """, (horse_name, race_date, race_date)).fetchall()

    if not rows:
        result = {"score": 55.0, "note": "調教データなし", "has_data": False}
        _training_score_cache[key] = result
        return result

    best = rows[0]
    lap1 = best['lap1'] or 99
    lap2 = best['lap2'] or 99

    # スコア計算:
    #   Lap1≤11.8 かつ Lap2≤12.0 → 95点（超絶仕上がり）
    #   Lap1≤12.0 かつ Lap2≤12.2 → 85点（好仕上がり）
    #   Lap1≤12.4 かつ Lap2≤12.4 → 75点（良好）← このCSVの基準
    #   Lap1≤12.6               → 60点（普通）
    #   それ以上                 → 50点（やや重め）
    if   lap1 <= 11.8 and lap2 <= 12.0:
        score = 95.0
    elif lap1 <= 12.0 and lap2 <= 12.2:
        score = 85.0
    elif lap1 <= 12.2 and lap2 <= 12.3:
        score = 78.0
    elif lap1 <= 12.4 and lap2 <= 12.4:
        score = 72.0
    elif lap1 <= 12.6:
        score = 60.0
    else:
        score = 50.0

    result = {"score": round(score, 1), "lap1": lap1, "lap2": lap2, "has_data": True}
    _training_score_cache[key] = result
    return result


def score_training_proxy(prev_time_sec, prev_distance,
                         prev_surface: str, conn) -> dict:
    """後方互換: 調教実データがない場合のフォールバック"""
    if pd.isna(prev_time_sec) or not prev_distance:
        return {"score": 55.0, "note": "前走タイムなし"}
    avg_row = conn.execute("""
        SELECT AVG(time_sec) as avg_t FROM results
        WHERE surface=? AND distance BETWEEN ? AND ?
          AND finish=1 AND time_sec IS NOT NULL
    """, (prev_surface or '芝', int(prev_distance)-100, int(prev_distance)+100)).fetchone()
    if avg_row and avg_row['avg_t']:
        diff = float(prev_time_sec) - avg_row['avg_t']
        score = max(0, min(100, 70 - diff * 10))
    else:
        score = 55.0
    return {"score": round(score, 1)}


# ══════════════════════════════════════════════════════════
# 5b. 初ダート／初芝 転向評価
# ══════════════════════════════════════════════════════════
_surface_switch_cache: dict = {}  # (horse_name, race_date[:7], surface) → dict or None

def score_surface_switch(horse_name: str, race_date: str,
                         surface: str, distance: int, conn,
                         sire: str = '', dam_sire: str = '') -> dict | None:
    """
    初ダート・初芝の馬を検出し、反対コース成績から換算スコアを返す。
    転向でない場合は None を返す。

    返り値:
        {"past_adj": float, "course_adj": float, "detail": str}
        past_adj / course_adj: score_past / score_course に加算する補正値
    """
    ym = race_date[:7]
    ck = (horse_name, ym, surface)
    if ck in _surface_switch_cache:
        return _surface_switch_cache[ck]

    # 今回の surface での過去走数
    same_cnt = conn.execute(
        "SELECT COUNT(*) FROM results WHERE horse_name=? AND date<? AND surface=? AND finish<90",
        (horse_name, race_date, surface)
    ).fetchone()[0]

    if same_cnt > 0:
        # 転向ではない（同surface経験あり）
        _surface_switch_cache[ck] = None
        return None

    # 反対surface
    opp_surface = 'ダ' if surface == '芝' else '芝'
    rows = conn.execute("""
        SELECT finish, last3f, num_horses, race_name, distance, odds
        FROM results
        WHERE horse_name=? AND date<? AND surface=? AND finish<90
        ORDER BY date DESC LIMIT 10
    """, (horse_name, race_date, opp_surface)).fetchall()

    if not rows:
        _surface_switch_cache[ck] = None
        return None

    rows = [dict(r) for r in rows]
    n = len(rows)

    # ── 反対surface での実力評価 ──
    wins = sum(1 for r in rows if r['finish'] == 1)
    top3 = sum(1 for r in rows if r['finish'] <= 3)
    avg_finish = sum(r['finish'] for r in rows) / n

    # クラスレベル判定（重賞実績 × 着順で重み付け）
    grade_bonus = 0.0
    for r in rows:
        rn = r.get('race_name', '') or ''
        fin = r['finish']
        # 着順による重み: 1着=1.0, 2着=0.8, 3着=0.6, 4-5着=0.4, 6着以下=0.2
        if   fin == 1: fin_w = 1.0
        elif fin == 2: fin_w = 0.8
        elif fin == 3: fin_w = 0.6
        elif fin <= 5: fin_w = 0.4
        else:          fin_w = 0.2
        if   'G1' in rn: grade_bonus = max(grade_bonus, 12.0 * fin_w)
        elif 'G2' in rn: grade_bonus = max(grade_bonus,  8.0 * fin_w)
        elif 'G3' in rn: grade_bonus = max(grade_bonus,  5.0 * fin_w)
        elif '(L)' in rn or 'OP' in rn: grade_bonus = max(grade_bonus, 3.0 * fin_w)

    # 基礎スコア: 反対surfaceでの成績を0.6掛けで換算
    raw = wins / n * 60 + top3 / n * 40
    base = min(100, raw * 1.5 + 30) * 0.6

    # 平均着順による補正
    if avg_finish <= 2.0:   base += 8
    elif avg_finish <= 3.5: base += 4
    elif avg_finish >= 8.0: base -= 5

    # 重賞実績加算
    base += grade_bonus

    # ── 血統ダート/芝適性 ──
    # sire のtarget surface での成績を参照
    blood_adj = 0.0
    if sire:
        dist_bucket = (distance // 400) * 400
        sire_row = conn.execute(
            "SELECT score FROM bloodline_stats WHERE col_type='sire' AND name=? AND surface=? AND dist_bucket=?",
            (sire, surface, dist_bucket)
        ).fetchone()
        if sire_row:
            sire_sc = sire_row['score']
            # 血統閾値（bloodline_statsのスコア分布: 中央値≒55, σ≒12）
            if   sire_sc >= 75: blood_adj = 5.0   # 上位10%: 明確なsurface適性
            elif sire_sc >= 65: blood_adj = 3.0   # 上位25%
            elif sire_sc >= 55: blood_adj = 0.0   # 中央値付近: 判断不能
            elif sire_sc >= 45: blood_adj = -3.0  # 下位25%
            else:               blood_adj = -5.0  # 下位10%: 明確に不向き

    # past_adj: score_past のデフォルト50点からの補正（±10キャップ）
    past_adj = max(-10.0, min(10.0, base - 50.0 + blood_adj))
    # course_adj: score_course のデフォルトからの補正（±5キャップ）
    course_adj = max(-5.0, min(5.0, (base - 50.0) * 0.5 + blood_adj * 0.5))

    detail = f"初{'ダート' if surface == 'ダ' else '芝'}（{opp_surface}{n}走: {wins}勝 top3={top3}）"
    result = {
        "past_adj": round(past_adj, 1),
        "course_adj": round(course_adj, 1),
        "detail": detail,
        "opp_wins": wins,
        "opp_top3": top3,
        "opp_n": n,
        "grade_bonus": grade_bonus,
        "blood_adj": blood_adj,
    }
    _surface_switch_cache[ck] = result
    return result


# ══════════════════════════════════════════════════════════
# 6. 血統スコア（父・母父）
# ══════════════════════════════════════════════════════════
# 血統スコアキャッシュ: (sire_name, col, year_half, surface, dist_bucket) → score
_bloodline_cache: dict = {}

_bloodline_score_cache: dict = {}  # (col_type, name, surface, dist_bucket) → score

def score_bloodline(sire: str, dam_sire: str, race_date: str,
                    surface: str, distance: int, conn) -> dict:
    """
    bloodline_stats テーブルから父・母父スコアを即座に取得
    メモリキャッシュ付き（2回目以降は0.001ms/回）
    """
    dist_bucket = (distance // 400) * 400

    def lookup(col_type, name):
        if not name: return 55.0
        ck = (col_type, name, surface, dist_bucket)
        if ck in _bloodline_score_cache:
            return _bloodline_score_cache[ck]
        row = conn.execute(
            "SELECT score FROM bloodline_stats WHERE col_type=? AND name=? AND surface=? AND dist_bucket=?",
            (col_type, name, surface, dist_bucket)
        ).fetchone()
        if row:
            _bloodline_score_cache[ck] = row['score']
            return row['score']
        # 距離バケット不一致 → 同コース全距離平均
        row2 = conn.execute(
            "SELECT AVG(score) FROM bloodline_stats WHERE col_type=? AND name=? AND surface=?",
            (col_type, name, surface)
        ).fetchone()
        val = row2[0] if row2 and row2[0] else 55.0
        _bloodline_score_cache[ck] = val
        return val

    sire_score    = lookup('sire',     sire)
    damsire_score = lookup('dam_sire', dam_sire)

    return {"score": round(sire_score, 1), "dam_sire": round(damsire_score, 1)}



# ══════════════════════════════════════════════════════════
# 血統×コース適性 相乗ボーナス
# ══════════════════════════════════════════════════════════
_course_blood_cache: dict = {}  # (horse_name, venue, surface, distance, ym) -> course_pct

def calc_course_blood_bonus(
    horse_name: str, race_date: str,
    venue: str, surface: str, distance: int,
    blood_rank: int, conn
) -> float:
    """
    血統ランク1位 AND コース適性上位の馬に加点。

    根拠（7年分・重賞G2/G3 685R）:
      血統1位 AND コース1位 → 3着内率48.0%（現◎比+3.5pt）
      相関係数0.082 → 独立した情報で本物の相乗効果

    距離別ボーナス:
      芝〜1700m (短距離・マイル): +5.0点
      芝2400m+  (長距離):        +3.0点
      芝中距離・ダート:            0点（効果なし）

    発動条件: 血統ランク1位 AND 同コース3着内率40%以上
    """
    if surface != '芝':
        return 0.0
    if distance <= 1700:
        bonus_base = 5.0
    elif distance >= 2400:
        bonus_base = 3.0
    else:
        return 0.0

    if blood_rank > 1:
        return 0.0

    # コース適性（キャッシュ付き）
    ym = race_date[:7]
    ck = (horse_name, venue, surface, distance, ym)
    if ck not in _course_blood_cache:
        rows = conn.execute(
            "SELECT finish FROM results "
            "WHERE horse_name=? AND venue=? AND surface=? "
            "AND distance BETWEEN ? AND ? AND date<? AND finish<90 "
            "ORDER BY date DESC LIMIT 10",
            (horse_name, venue, surface, distance-400, distance+400, race_date)
        ).fetchall()
        if len(rows) >= 2:
            pct = sum(1 for r in rows if r['finish'] <= 3) / len(rows) * 100
        else:
            pct = -1.0  # データ不足
        _course_blood_cache[ck] = pct
    else:
        pct = _course_blood_cache[ck]

    if pct >= 40.0:
        return bonus_base
    return 0.0



# ══════════════════════════════════════════════════════════
# 枠順・馬場条件×血統 加点（gate_cond_blood_bonus テーブル）
# ══════════════════════════════════════════════════════════
_gcbb_cache: dict = {}  # (venue, surface, distance, gate_cat, track_cond, sire) -> bonus
_gcbb_loaded: bool = False  # 一括ロード済みフラグ

def _load_gcbb_all(conn):
    """gate_cond_blood_bonusテーブルを全件一括ロード（初回のみ）"""
    global _gcbb_loaded
    if _gcbb_loaded: return
    rows = conn.execute(
        'SELECT venue,surface,distance,gate_cat,track_cond,sire,bonus FROM gate_cond_blood_bonus'
    ).fetchall()
    for r in rows:
        _gcbb_cache[(r[0],r[1],r[2],r[3],r[4],r[5])] = r[6]
    _gcbb_loaded = True

def calc_gate_cond_blood_bonus(
    horse_name: str, race_date: str,
    venue: str, surface: str, distance: int,
    horse_num: int, num_horses: int,
    track_cond: str, sire: str, conn
) -> float:
    """
    枠順・馬場×血統の加点（gate_cond_blood_bonusテーブルから一括ロード済み辞書を参照）
    DBクエリなし・0.0002ms/回で高速動作。
    """
    if not sire or horse_num <= 0 or num_horses <= 0:
        return 0.0

    # 初回のみ全件ロード
    if not _gcbb_loaded:
        _load_gcbb_all(conn)

    ratio = horse_num / num_horses
    if   ratio <= 0.35: gc = '内枠'
    elif ratio >= 0.65: gc = '外枠'
    else:               gc = '中枠'

    bonuses = []
    for gate_key, cond_key in [
        (gc, '全'),           # 枠×全馬場
        ('全枠', track_cond), # 全枠×当日馬場
        ('全枠', '全'),       # 全枠×全馬場
    ]:
        b = _gcbb_cache.get((venue, surface, distance, gate_key, cond_key, sire), 0.0)
        if b > 0: bonuses.append(b)

    return max(bonuses) if bonuses else 0.0



# ══════════════════════════════════════════════════════════
# 開催週トラックバイアス加点
# USE_TRACK_BIAS = False で無効化可能
# ══════════════════════════════════════════════════════════
USE_TRACK_BIAS = True   # ← False にすると即座に無効化

_tbb_cache:  dict = {}   # (venue,surface,phase,gate_cat,style_cat) -> bonus
_week_cache: dict = {}   # (venue, date) -> week番号

def _get_opening_week(venue: str, race_date: str, conn) -> int:
    """その日が同会場の開催セッションで何週目かを返す"""
    ck = (venue, race_date)
    if ck in _week_cache:
        return _week_cache[ck]

    # 同会場の過去90日以内の開催日一覧を取得
    rows = conn.execute(
        "SELECT DISTINCT date FROM results "
        "WHERE venue=? AND date<=? AND date>=date(?, '-90 days') AND finish<90 "
        "ORDER BY date",
        (venue, race_date, race_date)
    ).fetchall()

    if not rows:
        _week_cache[ck] = 1
        return 1

    from datetime import datetime
    dates = [r['date'] for r in rows]
    # セッション特定（21日以上空いたら新セッション）
    session_start = dates[0]
    for i in range(1, len(dates)):
        gap = (datetime.strptime(dates[i],'%Y-%m-%d') -
               datetime.strptime(dates[i-1],'%Y-%m-%d')).days
        if gap > 21:
            session_start = dates[i]

    # セッション内の週番号
    session_dates = [d for d in dates if d >= session_start]
    wn = 1; prev_d = None
    week_map = {}
    for d in sorted(set(session_dates)):
        if prev_d:
            gap = (datetime.strptime(d,'%Y-%m-%d') -
                   datetime.strptime(prev_d,'%Y-%m-%d')).days
            if gap >= 6:
                wn += 1
        week_map[d] = wn
        prev_d = d

    result = week_map.get(race_date, wn)
    _week_cache[ck] = result
    return result


def calc_track_bias_bonus(
    venue: str, surface: str, race_date: str,
    horse_num: int, num_horses: int,
    pos4: int, conn
) -> float:
    """
    開催週フェーズ × 枠順 × 脚質（4コーナー位置）の組み合わせで加点。

    根拠（7年分・29万頭）:
      函館ダ中盤・外枠先団: 3着内率63.4%（全体比+38.4pt）
      track_bias_bonusテーブル（429行）から取得

    発動条件: USE_TRACK_BIAS = True かつ pos4 > 0
    加点値: diff/3（最大±5点）、マイナスも付与（不利条件は減点）
    """
    if not USE_TRACK_BIAS:
        return 0.0
    if horse_num <= 0 or num_horses <= 0 or not pos4 or pos4 <= 0:
        return 0.0

    # フェーズ判定
    week = _get_opening_week(venue, race_date, conn)
    if   week <= 3: phase = '前半'
    elif week <= 5: phase = '中盤'
    else:           phase = '後半'

    # 枠カテゴリ
    ratio = horse_num / num_horses
    gate  = '内枠' if ratio <= 0.35 else ('外枠' if ratio >= 0.65 else '中枠')

    # 脚質カテゴリ（前走の4コーナー位置から推定）
    p4r = pos4 / num_horses
    style = '先団' if p4r <= 0.33 else ('後方' if p4r >= 0.67 else '中団')

    ck = (venue, surface, phase, gate, style)
    if ck not in _tbb_cache:
        row = conn.execute(
            "SELECT bonus FROM track_bias_bonus "
            "WHERE venue=? AND surface=? AND phase=? AND gate_cat=? AND style_cat=?",
            (venue, surface, phase, gate, style)
        ).fetchone()
        _tbb_cache[ck] = row['bonus'] if row else 0.0
    return _tbb_cache[ck]



# ══════════════════════════════════════════════════════════
# 会場×距離×父 ボーナス（venue_sire_bonus テーブル）
# 全体平均との乖離が大きい血統×コースパターンに加点
# ══════════════════════════════════════════════════════════
_vsb_cache: dict = {}   # {(venue, distance, sire): bonus}
_vsb_loaded: bool = False

def _load_vsb_all(conn):
    """venue_sire_bonusテーブルを全件一括ロード"""
    global _vsb_loaded
    if _vsb_loaded: return
    try:
        rows = conn.execute('SELECT venue, distance, sire, bonus FROM venue_sire_bonus').fetchall()
        for r in rows:
            _vsb_cache[(r['venue'], r['distance'], r['sire'])] = r['bonus']
        _vsb_loaded = True
    except:
        _vsb_loaded = True  # テーブルなくてもエラーにしない

def calc_venue_sire_bonus(venue: str, distance: int, sire: str, conn) -> float:
    """会場×距離×父のボーナスを返す（0〜5pt）"""
    if not _vsb_loaded:
        _load_vsb_all(conn)
    return _vsb_cache.get((venue, distance, sire.strip()), 0.0)


# ══════════════════════════════════════════════════════════
# 会場×距離×母父 ボーナス（venue_damsire_bonus テーブル）
# ══════════════════════════════════════════════════════════
_vdsb_cache: dict = {}
_vdsb_loaded: bool = False

def _load_vdsb_all(conn):
    """venue_damsire_bonusテーブルを全件一括ロード"""
    global _vdsb_loaded
    if _vdsb_loaded: return
    try:
        rows = conn.execute('SELECT venue, distance, dam_sire, bonus FROM venue_damsire_bonus').fetchall()
        for r in rows:
            _vdsb_cache[(r['venue'], r['distance'], r['dam_sire'])] = r['bonus']
        _vdsb_loaded = True
    except:
        _vdsb_loaded = True

def calc_venue_damsire_bonus(venue: str, distance: int, dam_sire: str, conn) -> float:
    """会場×距離×母父のボーナスを返す（0〜4pt）"""
    if not _vdsb_loaded:
        _load_vdsb_all(conn)
    return _vdsb_cache.get((venue, distance, dam_sire.strip()), 0.0)


# ══════════════════════════════════════════════════════════
# 4コーナー先頭予測ボーナス
# 過去走のpos4から「4コーナーで先頭グループにいる確率」を推定
# 先頭確率が高い馬 → 勝率17-20%/複勝率42-54%で圧倒的に強い
# ══════════════════════════════════════════════════════════
_front4_cache: dict = {}  # (horse_name, ym) -> front4_rate

def calc_front4_bonus(horse_name: str, race_date: str, prev_runs: list) -> float:
    """4コーナー先頭予測ボーナス（0〜4pt）

    過去3走のpos4/num_horsesから「4コーナー先頭率」を推定。
    先頭率50%+ → +4pt, 30%+ → +2pt
    データ検証: 4コーナー先頭の複勝率42-54%（全体比+20pt以上）
    """
    ym = race_date[:7]
    ck = (horse_name, ym)
    if ck in _front4_cache:
        return _front4_cache[ck]

    if not prev_runs:
        _front4_cache[ck] = 0.0
        return 0.0

    # 過去3走でpos4が上位25%以内だった割合
    front_count = 0
    valid = 0
    for run in prev_runs[:3]:
        pos4 = run.get('pos4', 0)
        num = run.get('num_horses', 0)
        if pos4 > 0 and num >= 6:
            valid += 1
            if pos4 / num <= 0.25:
                front_count += 1

    if valid == 0:
        _front4_cache[ck] = 0.0
        return 0.0

    front_rate = front_count / valid

    if front_rate >= 0.67:    # 3走中2走以上で先頭
        bonus = 4.0
    elif front_rate >= 0.34:  # 3走中1走で先頭
        bonus = 2.0
    else:
        bonus = 0.0

    _front4_cache[ck] = bonus
    return bonus


# ══════════════════════════════════════════════════════════
# 枠番グループ×父 ボーナス（gate_sire_bonus テーブル）
# ══════════════════════════════════════════════════════════
_gsb_cache: dict = {}
_gsb_loaded: bool = False

def _load_gsb_all(conn):
    global _gsb_loaded
    if _gsb_loaded: return
    try:
        rows = conn.execute('SELECT gate_group, sire, bonus FROM gate_sire_bonus').fetchall()
        for r in rows:
            _gsb_cache[(r['gate_group'], r['sire'])] = r['bonus']
        _gsb_loaded = True
    except:
        _gsb_loaded = True

def calc_gate_sire_bonus(horse_num: int, num_horses: int, sire: str, conn) -> float:
    """枠番グループ×父のボーナスを返す（0〜4pt）"""
    if not _gsb_loaded:
        _load_gsb_all(conn)
    if horse_num <= 0 or num_horses <= 0:
        return 0.0
    ratio = horse_num / num_horses
    if ratio <= 0.375:   gg = '内(1-3枠)'
    elif ratio <= 0.750: gg = '中(4-6枠)'
    else:                gg = '外(7-8枠)'
    return _gsb_cache.get((gg, sire.strip()), 0.0)


# ══════════════════════════════════════════════════════════
# 馬場(重/不良)×父 ボーナス（track_cond_sire_bonus テーブル）
# ══════════════════════════════════════════════════════════
_tcsb_cache: dict = {}
_tcsb_loaded: bool = False

def _load_tcsb_all(conn):
    global _tcsb_loaded
    if _tcsb_loaded: return
    try:
        rows = conn.execute('SELECT track_cond, sire, bonus FROM track_cond_sire_bonus').fetchall()
        for r in rows:
            _tcsb_cache[(r['track_cond'], r['sire'])] = r['bonus']
        _tcsb_loaded = True
    except:
        _tcsb_loaded = True

def calc_track_cond_sire_bonus(track_cond: str, sire: str, conn) -> float:
    """馬場(重/不良)×父のボーナスを返す（0〜4pt）"""
    if not _tcsb_loaded:
        _load_tcsb_all(conn)
    if track_cond not in ('重', '不'):
        return 0.0
    cond_label = '重' if track_cond == '重' else '不良'
    return _tcsb_cache.get((cond_label, sire.strip()), 0.0)


# ══════════════════════════════════════════════════════════
# 前走上がり3F順位 加点
# ══════════════════════════════════════════════════════════
def calc_last3f_rank_bonus(past_runs: list) -> float:
    """
    前走の上がり3F順位（同レース内）から加点/減点。

    根拠（7年分・254,673頭）:
      前走 top20% → 次走3着内率29.1%（全体比+7.1pt）→ +2.4点
      前走 top40% → 次走3着内率25.5%（全体比+3.6pt）→ +1.2点
      前走 mid60% → ほぼ変化なし                       → −0.3点
      前走 bot40% → 次走3着内率13.4%（全体比−8.6pt）→ −2.9点

    長距離・中距離で特に有効。
    直近1走のデータを使用。
    """
    if not past_runs:
        return 0.0

    prev = past_runs[0]  # 直近1走
    l3f_rank   = prev.get('l3f_rank')
    num_horses = prev.get('num_horses', 0) or 0

    if l3f_rank is None or num_horses < 4:
        return 0.0

    ratio = l3f_rank / num_horses
    if   ratio <= 0.20: return  2.4   # top20%
    elif ratio <= 0.40: return  1.2   # top40%
    elif ratio <= 0.60: return -0.3   # mid60%
    else:               return -2.9   # bot40%


def score_race(df_race: pd.DataFrame, race_date: str,
               surface: str, distance: int, track_cond: str,
               grade: str = '', venue: str = '', db_path: str = DB_PATH) -> pd.DataFrame:
    """
    出走馬リストを受け取り、全馬のスコアを算出して返す

    Args:
        df_race:    出走馬DataFrame（馬名・騎手・調教師・前走データ等）
        race_date:  レース日（'2026-03-29'形式）
        surface:    '芝' or 'ダ'
        distance:   距離（例: 1200）
        track_cond: '良'|'稍重'|'重'|'不良'
        grade:      クラス（例: 'G1'）
        venue:      開催場所（例: '中京'）← 枠順・脚質スコアに使用

    Returns:
        スコア追記済みDataFrame（スコア降順ソート）
    """
    conn = get_conn(db_path)
    results = []

    for _, row in df_race.iterrows():
        horse  = str(row.get('horse_name', row.get('馬名', ''))).strip()
        jockey = str(row.get('jockey',     row.get('騎手', ''))).strip()
        trainer= str(row.get('trainer',    row.get('調教師', ''))).strip()
        sire   = str(row.get('sire',       row.get('種牡馬', ''))).strip()
        dam_sire = str(row.get('dam_sire', row.get('母父馬', ''))).strip()

        interval  = row.get('interval_weeks', row.get('間隔'))
        prev_fin  = row.get('prev_finish',    row.get('前走着順'))
        prev_dist = row.get('prev_distance',  row.get('前距離'))
        prev_surf = row.get('prev_surface',   row.get('前芝・ダ', '芝'))
        prev_time = row.get('prev_time_sec',  None)
        if prev_time is None:
            raw = row.get('prev_time_raw', row.get('前走走破タイム'))
            if pd.notna(raw):
                from build_db import parse_time_to_sec
                prev_time = parse_time_to_sec(raw)

        horse_num = int(row.get('horse_num', 0)) if pd.notna(row.get('horse_num', 0)) else 0

        # 各ファクタースコア
        s_past   = score_past_performance(horse, race_date, surface, distance, conn)
        s_course = score_course_fitness(horse, race_date, surface, distance, track_cond, conn)
        s_jt     = score_jockey_trainer(jockey, trainer, race_date, surface, distance, conn, horse, grade=grade)
        s_rot    = score_rotation(interval, prev_fin, prev_dist, distance, grade)
        s_train  = score_training_proxy(prev_time, prev_dist, prev_surf, conn)
        s_blood  = score_bloodline(sire, dam_sire, race_date, surface, distance, conn)
        s_gs     = score_gate_style(horse, horse_num, race_date, venue, surface, distance, conn)

        # 初ダート/初芝 転向補正
        s_switch = score_surface_switch(horse, race_date, surface, distance, conn,
                                        sire=sire, dam_sire=dam_sire)
        if s_switch:
            s_past['score']   = max(0, min(100, s_past['score']   + s_switch['past_adj']))
            s_course['score'] = max(0, min(100, s_course['score'] + s_switch['course_adj']))

        # レース種別でウェイト切替（新馬・未勝利は血統・調教優先）
        W = get_weights(grade)

        # 重み付き合算
        total = (
            s_past['score']     * W['past_performance'] +
            s_course['score']   * W['course_fitness'] +
            s_jt['score']       * W['jockey_trainer'] +
            s_rot['score']      * W['rotation'] +
            s_train['score']    * W['training'] +
            s_blood['score']    * W['sire'] +
            s_blood['dam_sire'] * W['dam_sire'] +
            s_gs['score']       * W['gate_style']
        )

        results.append({
            **row.to_dict(),
            'total_score':      round(total, 1),
            'score_past':       s_past['score'],
            'score_course':     s_course['score'],
            'score_jt':         s_jt['score'],
            'score_rotation':   s_rot['score'],
            'score_training':   s_train['score'],
            'score_sire':       s_blood['score'],
            'score_dam_sire':   s_blood['dam_sire'],
            'score_gate_style': s_gs['score'],
            'running_style':    s_gs['style'],
            'gate_diff':        s_gs['gate_diff'],
            'style_diff':       s_gs['style_diff'],
            'past_data_n':      s_past['n'],
            'course_data_n':    s_course['n'],
            'surface_switch':   s_switch['detail'] if s_switch else '',
            'switch_past_adj':  s_switch['past_adj'] if s_switch else 0.0,
            'switch_course_adj':s_switch['course_adj'] if s_switch else 0.0,
        })

    conn.close()
    df_out = pd.DataFrame(results)
    df_out = df_out.sort_values('total_score', ascending=False).reset_index(drop=True)
    df_out['score_rank'] = df_out.index + 1

    # 突出度
    if len(df_out) >= 2:
        gap = df_out['total_score'].iloc[0] - df_out['total_score'].iloc[1]
        df_out['standout_gap'] = round(gap, 1)
    else:
        df_out['standout_gap'] = 0.0

    # 印付け
    df_out['mark'] = df_out['score_rank'].apply(
        lambda r: '◎' if r==1 else '○' if r==2 else '▲' if r==3 else '△' if r<=5 else '—'
    )

    return df_out


# ══════════════════════════════════════════════════════════
# 期待値計算
# ══════════════════════════════════════════════════════════
def calc_ev(df: pd.DataFrame, odds_col: str = 'odds') -> pd.DataFrame:
    """
    スコアからソフトマックスで推定勝率を算出 → 期待値計算
    
    - win_prob_pct : 推定勝率（%）
    - expected_value: 期待値（%）= 推定勝率 × オッズ - 1 を%表示
    - ev_ok / ev_reasons: is_ev_race の結果（本命馬のみ）
    """
    df = df.copy()

    # ソフトマックスで推定勝率
    scores = df['total_score'].values
    exp_s  = np.exp((scores - scores.mean()) / 10)  # scale=10: 接戦均等化とEV異常のバランス
    probs  = exp_s / exp_s.sum()
    df['win_prob']     = probs
    df['win_prob_pct'] = (probs * 100).round(1)

    # 期待値計算（オッズがある行のみ）
    if odds_col in df.columns:
        odds_vals = pd.to_numeric(df[odds_col], errors='coerce')
        # ★ EV異常値修正: オッズ上限フィルター
        # 30倍超はsoftmax確率推定が破綻するためEV計算対象外（NaNとして表示）
        MAX_ODDS_FOR_EV = EV_CONDITIONS['max_odds']
        odds_for_ev = odds_vals.where(odds_vals <= MAX_ODDS_FOR_EV, other=np.nan)
        df['expected_value'] = ((probs * odds_for_ev - 1) * 100).round(1)
        df['odds_filtered'] = odds_vals > MAX_ODDS_FOR_EV  # フィルタ済みフラグ
    else:
        df['expected_value'] = np.nan

    # 本命（rank1）の期待値判定を各行に付与
    honmei_row = df.iloc[0]
    gap  = float(df['standout_gap'].iloc[0]) if 'standout_gap' in df.columns else 0.0
    h_odds = None
    if odds_col in df.columns:
        raw = honmei_row.get(odds_col)
        if pd.notna(raw):
            h_odds = float(raw)
    h_prob = float(honmei_row['win_prob'])

    ev_ok, ev_reasons = is_ev_race(gap, h_odds, h_prob)
    df['ev_ok']      = ev_ok
    df['ev_reasons'] = [ev_reasons] * len(df)

    return df


# ══════════════════════════════════════════════════════════
# JSON出力（UI用）
# ══════════════════════════════════════════════════════════
def to_json(df: pd.DataFrame, race_meta: dict, out_path: str = 'race_result.json'):
    """スコアリング結果をUIが読めるJSON形式で出力"""
    horses = []
    for _, row in df.iterrows():
        horses.append({
            'score_rank':    int(row.get('score_rank', 0)),
            'mark':          row.get('mark', '—'),
            'horse_name':    row.get('horse_name', row.get('馬名', '')),
            'jockey':        row.get('jockey', row.get('騎手', '')),
            'trainer':       row.get('trainer', row.get('調教師', '')),
            'sire':          row.get('sire', row.get('種牡馬', '')),
            'dam_sire':      row.get('dam_sire', row.get('母父馬', '')),
            'total_score':   float(row.get('total_score', 0)),
            'win_prob_pct':  float(row.get('win_prob_pct', 0)),
            'expected_value':float(row.get('expected_value', 0)) if pd.notna(row.get('expected_value')) else None,
            'odds':          float(row.get('odds', row.get('単勝オッズ', 0))) if pd.notna(row.get('odds', row.get('単勝オッズ'))) else None,
            'sub_scores': {
                '過去成績':   float(row.get('score_past', 0)),
                'コース適性': float(row.get('score_course', 0)),
                '騎手厩舎':   float(row.get('score_jt', 0)),
                'ローテ':     float(row.get('score_rotation', 0)),
                '調教':       float(row.get('score_training', 0)),
                '父血統':     float(row.get('score_sire', 0)),
                '母父血統':   float(row.get('score_dam_sire', 0)),
            },
            'past_data_n':   int(row.get('past_data_n', 0)),
            'course_data_n': int(row.get('course_data_n', 0)),
        })

    output = {
        **race_meta,
        'standout_gap': float(df['standout_gap'].iloc[0]) if len(df) > 0 else 0,
        'is_ev_race': is_ev_race(
            float(df['standout_gap'].iloc[0]) if len(df) > 0 else 0,
            df.iloc[0].get('odds') if len(df) > 0 else None
        )[0],
        'ev_reasons': is_ev_race(
            float(df['standout_gap'].iloc[0]) if len(df) > 0 else 0,
            df.iloc[0].get('odds') if len(df) > 0 else None
        )[1],
        'horses': horses,
    }

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"  💾 JSON出力: {out_path}")
    return output
