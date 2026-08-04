# -*- coding: utf-8 -*-
"""
generate_race_sim.py
レースIDを指定してkeiba.dbから出走馬データを取得し、
race_sim_yasuda_v2.htmlを雛形にした展開シミュレーターHTMLを生成する。

使用例:
    python generate_race_sim.py --race-id 2026-06-07_東京_11
    python generate_race_sim.py --race-id 2026-06-07_東京_11 --open
    python generate_race_sim.py --this-week
"""

import sqlite3
import json
import argparse
import sys
import os
import re
import math
from datetime import datetime, timedelta
from pathlib import Path

# ══════════════════════════════════════════════════════
# 定数・デフォルト値
# ══════════════════════════════════════════════════════

DEFAULT_DB = "keiba.db"
MAX_RACES = 15          # 過去N走
MAX_YEARS = 2           # 過去N年以内

# ペース適性スタイル別デフォルト（委員会合意値）
STYLE_DEFAULTS = {
    "逃げ":    {"H": 40, "M": 70, "S": 65},
    "先行":    {"H": 60, "M": 75, "S": 55},
    "差し":    {"H": 75, "M": 65, "S": 50},
    "追い込み": {"H": 80, "M": 55, "S": 40},
}

# 新馬・外国馬などデータ不足時のデフォルト
FALLBACK_DEFAULTS = {"speed": 75, "sprint": 80, "stamina": 70, "pace": {"H": 60, "M": 70, "S": 60}}


# 騎手脚質傾向データ（jockey_pace_style.json）
_JOCKEY_STYLE: dict = {}

def _load_jockey_style() -> None:
    path = Path(__file__).parent / "jockey_pace_style.json"
    if not path.exists():
        return
    data = json.load(open(path, encoding="utf-8"))
    for r in data.get("jockey_stats", []):
        key = (r["jockey"], r["surface"])
        _JOCKEY_STYLE[key] = r

_load_jockey_style()


# ══════════════════════════════════════════════════════
# DB接続
# ══════════════════════════════════════════════════════

def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")
    return conn


# ══════════════════════════════════════════════════════
# レース情報取得
# ══════════════════════════════════════════════════════

def get_race_info(conn: sqlite3.Connection, race_id: str) -> dict | None:
    """
    race_idからレース情報を取得する。
    DB race_id形式: '2026-06-07_東京_11'
    """
    row = conn.execute("""
        SELECT race_id, date, venue, race_num, race_name, surface, distance,
               track_cond, num_horses
        FROM results
        WHERE race_id = ?
        LIMIT 1
    """, (race_id,)).fetchone()

    if row is None:
        return None

    return {
        "race_id":   row["race_id"],
        "date":      row["date"],
        "venue":     row["venue"],
        "race_num":  row["race_num"],
        "race_name": row["race_name"],
        "surface":   row["surface"],
        "distance":  row["distance"],
        "track_cond": row["track_cond"],
    }


def get_race_horses(conn: sqlite3.Connection, race_id: str) -> list[dict]:
    """
    race_idの出走馬一覧を取得する。
    resultsテーブルからUNIONで出走馬を収集。
    """
    rows = conn.execute("""
        SELECT horse_name, horse_num, umaban, jockey,
               finish, pos4, last3f, time_sec, num_horses,
               odds, popularity
        FROM results
        WHERE race_id = ?
        ORDER BY horse_num
    """, (race_id,)).fetchall()

    horses = []
    for r in rows:
        horses.append({
            "horse_name": r["horse_name"].strip() if r["horse_name"] else "",
            "horse_num":  r["horse_num"],
            "umaban":     r["umaban"],
            "jockey":     r["jockey"].strip() if r["jockey"] else "",
            "finish":     r["finish"],
            "pos4":       r["pos4"],
            "last3f":     r["last3f"],
            "time_sec":   r["time_sec"],
            "num_horses": r["num_horses"],
            "odds":       r["odds"],
            "popularity": r["popularity"],
        })
    return horses


def get_race_horses_from_json(json_race: dict) -> list[dict]:
    """
    this_week_races.jsonのレースデータから出走馬一覧を取得する（未来レース用）。
    """
    horses = []
    for h in json_race.get("horses", []):
        waku = h.get("waku", 0)
        umaban = h.get("umaban", 0)
        horses.append({
            "horse_name": h.get("name", "").strip(),
            "horse_num":  umaban,
            "umaban":     umaban,
            "waku":       waku,
            "jockey":     h.get("jockey", "").strip(),
            "finish":     None,
            "pos4":       None,
            "last3f":     None,
            "time_sec":   None,
            "num_horses": len(json_race.get("horses", [])),
            "odds":       float(h.get("odds", 0)) if h.get("odds") else None,
            "popularity": h.get("popularity"),
        })
    return horses


# ══════════════════════════════════════════════════════
# Par タイム計算（A: スピード指数ベース）
# ══════════════════════════════════════════════════════

def get_par_time(conn: sqlite3.Connection, venue: str, surface: str, distance: int) -> float | None:
    """
    venue × distance × surface の勝ち馬上位3頭平均タイムを取得。
    n < 10 の場合は distance × surface のみで計算。
    """
    # 上位3頭平均のCTE
    row = conn.execute("""
        WITH ranked AS (
            SELECT race_id, time_sec,
                   ROW_NUMBER() OVER (PARTITION BY race_id ORDER BY finish) as rn
            FROM results
            WHERE venue = ? AND surface = ? AND distance = ?
              AND finish < 90 AND time_sec > 0
        )
        SELECT AVG(time_sec) as avg_top3, COUNT(*) as n
        FROM ranked
        WHERE rn <= 3
    """, (venue, surface, distance)).fetchone()

    if row and row["n"] is not None and row["n"] >= 10:
        return row["avg_top3"]

    # fallback: distance × surface のみ
    row2 = conn.execute("""
        WITH ranked AS (
            SELECT race_id, time_sec,
                   ROW_NUMBER() OVER (PARTITION BY race_id ORDER BY finish) as rn
            FROM results
            WHERE surface = ? AND distance = ?
              AND finish < 90 AND time_sec > 0
        )
        SELECT AVG(time_sec) as avg_top3, COUNT(*) as n
        FROM ranked
        WHERE rn <= 3
    """, (surface, distance)).fetchone()

    if row2 and row2["n"] is not None and row2["n"] >= 5:
        return row2["avg_top3"]

    return None


def calc_speed_index(horse_time: float | None, par_time: float | None) -> float:
    """
    スピード指数 = (par_time - horse_time) / par_time * 500 + 80
    委員会合意: 50-110のハードクリップ
    """
    if horse_time is None or par_time is None or par_time == 0:
        return 75.0  # デフォルト

    speed = (par_time - horse_time) / par_time * 500 + 80
    return max(50.0, min(110.0, speed))


# ══════════════════════════════════════════════════════
# 馬の過去走データ取得
# ══════════════════════════════════════════════════════

def get_horse_history(conn: sqlite3.Connection, horse_name: str, current_date: str,
                      surface: str, max_races: int = MAX_RACES,
                      max_years: int = MAX_YEARS) -> list[dict]:
    """
    馬の過去走データを取得する。
    - 同surface必須（fallback: surface問わず）
    - 直近max_races走・max_years年以内
    - race_lapsをLEFT JOINしてpace_type取得
    """
    cutoff_date = (datetime.strptime(current_date, "%Y-%m-%d")
                   - timedelta(days=365 * max_years)).strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT r.date, r.venue, r.surface, r.distance, r.finish, r.num_horses,
               r.pos4, r.last3f, r.time_sec, r.track_cond, r.race_id,
               rl.pace_type, rl.first_3f, rl.last_3f_race,
               r.race_name
        FROM results r
        LEFT JOIN race_laps rl ON rl.race_id = r.race_id
        WHERE TRIM(r.horse_name) = TRIM(?)
          AND r.date < ?
          AND r.date >= ?
          AND r.finish < 90
          AND r.surface = ?
        ORDER BY r.date DESC
        LIMIT ?
    """, (horse_name, current_date, cutoff_date, surface, max_races)).fetchall()

    if len(rows) < 5:
        # Fallback: surface問わず
        rows = conn.execute("""
            SELECT r.date, r.venue, r.surface, r.distance, r.finish, r.num_horses,
                   r.pos4, r.last3f, r.time_sec, r.track_cond, r.race_id,
                   rl.pace_type, rl.first_3f, rl.last_3f_race,
                   r.race_name
            FROM results r
            LEFT JOIN race_laps rl ON rl.race_id = r.race_id
            WHERE TRIM(r.horse_name) = TRIM(?)
              AND r.date < ?
              AND r.date >= ?
              AND r.finish < 90
            ORDER BY r.date DESC
            LIMIT ?
        """, (horse_name, current_date, cutoff_date, max_races)).fetchall()

    history = []
    for r in rows:
        history.append({
            "date":       r["date"],
            "venue":      r["venue"],
            "surface":    r["surface"],
            "distance":   r["distance"],
            "finish":     r["finish"],
            "num_horses": r["num_horses"],
            "pos4":       r["pos4"],
            "last3f":     r["last3f"],
            "time_sec":   r["time_sec"],
            "track_cond": r["track_cond"],
            "race_id":    r["race_id"],
            "pace_type":  r["pace_type"],
            "first_3f":   r["first_3f"],
            "last_3f_race": r["last_3f_race"],
            "race_name":  r["race_name"],
        })
    return history


# ══════════════════════════════════════════════════════
# 時系列重み
# ══════════════════════════════════════════════════════

def time_weight(race_date: str, current_date: str) -> float:
    """weight = 0.85 ^ years_ago（委員会合意）"""
    try:
        d = datetime.strptime(race_date, "%Y-%m-%d")
        c = datetime.strptime(current_date, "%Y-%m-%d")
        years_ago = (c - d).days / 365.0
        return 0.85 ** years_ago
    except Exception:
        return 1.0


# ══════════════════════════════════════════════════════
# A. スピード指数計算
# ══════════════════════════════════════════════════════

# par_timeキャッシュ（surface×distance×venueで重複計算を避ける）
_par_time_cache: dict = {}


def get_par_time_cached(conn: sqlite3.Connection, venue: str, surface: str, distance: int) -> float | None:
    key = (venue, surface, distance)
    if key not in _par_time_cache:
        _par_time_cache[key] = get_par_time(conn, venue, surface, distance)
    return _par_time_cache[key]


def calc_horse_speed(conn: sqlite3.Connection, history: list[dict], par_time: float | None,
                     current_date: str, target_surface: str, target_distance: int) -> float:
    """
    過去走の加重平均スピード指数を計算する。
    各過去走に対してそのvenue×surface×distanceのpar_timeを使う。
    これにより芝・ダート混在履歴でも正しく比較できる。
    """
    if not history:
        return FALLBACK_DEFAULTS["speed"]

    scores = []
    weights = []
    for h in history:
        if not h["time_sec"] or h["time_sec"] <= 0:
            continue
        # 各レースのpar_timeを使用
        run_par = get_par_time_cached(conn, h["venue"], h["surface"], h["distance"])
        if run_par is None:
            # 対象レースのpar_timeで代替
            run_par = par_time
        if run_par is None:
            continue
        si = calc_speed_index(h["time_sec"], run_par)
        w = time_weight(h["date"], current_date)
        scores.append(si)
        weights.append(w)

    if not scores:
        return FALLBACK_DEFAULTS["speed"]

    total_w = sum(weights)
    weighted_avg = sum(s * w for s, w in zip(scores, weights)) / total_w
    return max(50.0, min(110.0, weighted_avg))


# ══════════════════════════════════════════════════════
# B. スプリント指数計算
# ══════════════════════════════════════════════════════

def get_race_avg_last3f(conn: sqlite3.Connection, race_id: str) -> float | None:
    """race_idの全出走馬last3f平均を取得（WINDOW関数相当）"""
    row = conn.execute("""
        SELECT AVG(last3f) as avg_l3f
        FROM results
        WHERE race_id = ? AND last3f > 0
    """, (race_id,)).fetchone()
    return row["avg_l3f"] if row and row["avg_l3f"] else None


def calc_horse_sprint(history: list[dict], current_date: str) -> float:
    """
    スプリント指数 = 100 - (horse_last3f - race_avg_last3f) × 10
    直近1走weight=1.5, それ以前=1.0
    n<3の場合は固定65
    """
    if not history:
        return 65.0

    valid = [h for h in history if h["last3f"] and h["last3f"] > 0 and h.get("race_avg_l3f")]
    if len(valid) < 3:
        return 65.0

    scores = []
    weights = []
    for i, h in enumerate(valid):
        sprint = 100 - (h["last3f"] - h["race_avg_l3f"]) * 10
        sprint = max(50.0, min(105.0, sprint))
        # 直近1走は1.5倍、それ以前は1.0倍
        recency_w = 1.5 if i == 0 else 1.0
        time_w = time_weight(h["date"], current_date)
        scores.append(sprint)
        weights.append(recency_w * time_w)

    total_w = sum(weights)
    return max(50.0, min(105.0, sum(s * w for s, w in zip(scores, weights)) / total_w))


# ══════════════════════════════════════════════════════
# C. 脚質分類
# ══════════════════════════════════════════════════════

def classify_style(history: list[dict], target_distance: int,
                   jockey: str = "", surface: str = "芝") -> str:
    """
    脚質を分類する。
    - 対象距離±300m以内の成績で分類
    - avg_pos4/num_horses + pos4=1出現率の2次元分類
    - n<5の場合は '先行'（ベイズ事前分布）
    """
    if not history:
        return "先行"

    # 距離フィルタ
    dist_filtered = [h for h in history
                     if h["distance"] and abs(h["distance"] - target_distance) <= 300
                     and h["pos4"] and h["num_horses"] and h["num_horses"] > 0]

    if len(dist_filtered) < 5:
        # fallback: 全距離
        dist_filtered = [h for h in history
                         if h["pos4"] and h["num_horses"] and h["num_horses"] > 0]

    if len(dist_filtered) < 3:
        # 騎手傾向フォールバック
        jkey = (jockey, surface)
        if jkey in _JOCKEY_STYLE:
            return _JOCKEY_STYLE[jkey]["style"]
        return "先行"

    # avg_pos4 / num_horses
    ratios = [h["pos4"] / h["num_horses"] for h in dist_filtered]
    avg_ratio = sum(ratios) / len(ratios)

    # pos4=1出現率
    pos4_eq1_count = sum(1 for h in dist_filtered if h["pos4"] == 1)
    pos4_eq1_rate = pos4_eq1_count / len(dist_filtered)

    # 分類（委員会合意）
    if avg_ratio < 0.1 or pos4_eq1_rate > 0.40:
        return "逃げ"
    elif avg_ratio < 0.35:
        return "先行"
    elif avg_ratio < 0.60:
        return "差し"
    else:
        return "追い込み"


# ══════════════════════════════════════════════════════
# D. ペース適性計算
# ══════════════════════════════════════════════════════

def calc_pace_adaptability(history: list[dict], style: str,
                           current_date: str, target_distance: int) -> dict:
    """
    H/M/S ペース別の適性スコアを計算する。
    - finish_pct = (finish-1)/(num_horses-1)
    - pace_score = max(40, 100 - finish_pct * 80)
    - outlier除外: finish_pct > 0.85
    - n<3の場合はstyle_default使用
    - ベイズ縮小: weight = min(1.0, n_samples/10.0)
    """
    default = STYLE_DEFAULTS.get(style, STYLE_DEFAULTS["先行"])

    # 距離フィルタ（±300m）
    dist_filtered = [h for h in history
                     if h["distance"] and abs(h["distance"] - target_distance) <= 300
                     and h["pace_type"] in ("H", "M", "S")
                     and h["finish"] and h["num_horses"] and h["num_horses"] > 1]

    if len(dist_filtered) < 3:
        # fallback: 全距離
        dist_filtered = [h for h in history
                         if h["pace_type"] in ("H", "M", "S")
                         and h["finish"] and h["num_horses"] and h["num_horses"] > 1]

    # ペースタイプ別に集計
    pace_groups: dict[str, list] = {"H": [], "M": [], "S": []}
    for h in dist_filtered:
        pt = h["pace_type"]
        finish_pct = (h["finish"] - 1) / (h["num_horses"] - 1)
        if finish_pct > 0.85:
            continue  # outlier除外
        pace_score = max(40.0, 100.0 - finish_pct * 80.0)
        w = time_weight(h["date"], current_date)
        pace_groups[pt].append((pace_score, w))

    result = {}
    for pt in ("H", "M", "S"):
        grp = pace_groups[pt]
        n = len(grp)
        prior = default[pt]
        if n < 3:
            result[pt] = prior
        else:
            total_w = sum(w for _, w in grp)
            observed = sum(s * w for s, w in grp) / total_w
            # ベイズ縮小
            blend_w = min(1.0, n / 10.0)
            result[pt] = round(blend_w * observed + (1 - blend_w) * prior, 1)

    return result


# ══════════════════════════════════════════════════════
# E. スタミナ指数計算
# ══════════════════════════════════════════════════════

def calc_stamina(history: list[dict], speed: float, sprint: float,
                 current_date: str, target_distance: int) -> float:
    """
    スタミナ指数計算。
    - 距離±200m以内でn>=3かつHペース実績あり:
      stamina = max(40, 100 - h_pace_finish_pct * 80)
    - それ以外: speed×0.55 + sprint×0.45
    - 2000m以上: speed比重を0.65に引き上げ
    """
    # 距離±200m以内のHペース実績
    h_pace = [h for h in history
              if h["distance"] and abs(h["distance"] - target_distance) <= 200
              and h["pace_type"] == "H"
              and h["finish"] and h["num_horses"] and h["num_horses"] > 1]

    if len(h_pace) >= 3:
        finish_pcts = []
        for h in h_pace:
            fp = (h["finish"] - 1) / (h["num_horses"] - 1)
            if fp <= 0.85:
                finish_pcts.append(fp)
        if len(finish_pcts) >= 2:
            avg_fp = sum(finish_pcts) / len(finish_pcts)
            stamina = max(40.0, 100.0 - avg_fp * 80.0)
            return max(40.0, min(100.0, stamina))

    # Fallback: speed/sprint加重平均
    if target_distance >= 2000:
        stamina = speed * 0.65 + sprint * 0.35
    else:
        stamina = speed * 0.55 + sprint * 0.45

    return max(40.0, min(100.0, stamina))


# ══════════════════════════════════════════════════════
# 馬ごとの指数計算メイン
# ══════════════════════════════════════════════════════

def calc_horse_ratings(conn: sqlite3.Connection, horse: dict,
                       race_info: dict, par_time: float | None) -> dict:
    """
    1頭分の指数を計算して返す。
    """
    horse_name = horse["horse_name"]
    current_date = race_info["date"]
    surface = race_info["surface"]
    distance = race_info["distance"]

    # 過去走取得
    history = get_horse_history(conn, horse_name, current_date, surface)

    # race_avg_l3f を各過去走に付与
    for h in history:
        avg_l3f = get_race_avg_last3f(conn, h["race_id"])
        h["race_avg_l3f"] = avg_l3f

    n_races = len(history)
    umaban = horse["umaban"] or horse["horse_num"] or 1
    actual_waku = horse.get("waku") or 0
    gate = actual_waku if actual_waku > 0 else _umaban_to_gate(umaban)

    # データ不足の場合はデフォルト値
    if n_races < 3:
        defaults = FALLBACK_DEFAULTS.copy()
        jockey_name = horse.get("jockey", "")
        style = classify_style(history, distance, jockey=jockey_name, surface=surface) if history else "先行"
        style_default = STYLE_DEFAULTS.get(style, STYLE_DEFAULTS["先行"])
        return {
            "name":    horse_name,
            "no":      umaban,
            "jockey":  horse["jockey"],
            "gate":    gate,
            "style":   style,
            "speed":   defaults["speed"],
            "sprint":  defaults["sprint"],
            "stamina": defaults["stamina"],
            "pace":    style_default,
            "l3f":     horse.get("last3f") or 34.5,
            "odds":    horse.get("odds"),
        }

    # A. スピード指数
    speed = calc_horse_speed(conn, history, par_time, current_date, surface, distance)

    # B. スプリント指数
    sprint = calc_horse_sprint(history, current_date)

    # C. 脚質
    jockey_name = horse.get("jockey", "")
    style = classify_style(history, distance, jockey=jockey_name, surface=surface)

    # D. ペース適性
    pace = calc_pace_adaptability(history, style, current_date, distance)

    # E. スタミナ
    stamina = calc_stamina(history, speed, sprint, current_date, distance)

    # 代表上がり3F (直近有効l3f)
    l3f_vals = [h["last3f"] for h in history if h["last3f"] and h["last3f"] > 0]
    best_l3f = round(min(l3f_vals[:5]), 1) if l3f_vals else 34.5

    return {
        "name":    horse_name,
        "no":      umaban,
        "jockey":  horse["jockey"],
        "gate":    gate,
        "style":   style,
        "speed":   round(speed, 1),
        "sprint":  round(sprint, 1),
        "stamina": round(stamina, 1),
        "pace":    {k: round(v, 1) for k, v in pace.items()},
        "l3f":     best_l3f,
        "odds":    horse.get("odds"),
    }


def _umaban_to_gate(umaban: int | None) -> int:
    """馬番から枠番を計算（JRA 18頭以下標準）"""
    if not umaban or umaban <= 0:
        return 1
    gate = math.ceil(umaban / 2)
    return min(gate, 8)


# ══════════════════════════════════════════════════════
# ペースシナリオ生成
# ══════════════════════════════════════════════════════

def estimate_scenarios(race_info: dict, rated_horses: list[dict],
                       conn=None, x_nige_name: str | None = None) -> dict:
    """
    H/M/S/X のシナリオを推定する。
    - conn が渡された場合は race_laps の実績3Fタイムを優先使用
    - x_nige_name が指定された場合は X (大逃げ) シナリオを追加
    """
    surface = race_info["surface"]
    distance = race_info["distance"]
    venue = race_info.get("venue", "")

    # ── 1. DB実績3F取得 (race_laps) ─────────────────────────────────
    db_pace: dict[str, dict] = {}
    if conn is not None:
        try:
            # 優先: venue+surface+distance 完全一致 (n>=5)
            rows = conn.execute("""
                SELECT pace_type,
                       AVG(first_3f)       as f3,
                       AVG(last_3f_race)   as l3,
                       COUNT(*)            as n
                FROM race_laps
                WHERE venue=? AND surface=? AND distance=?
                  AND pace_type IS NOT NULL
                GROUP BY pace_type
            """, (venue, surface, distance)).fetchall()
            for row in rows:
                if row["n"] and row["n"] >= 5:
                    db_pace[row["pace_type"]] = {
                        "f3": row["f3"], "l3": row["l3"], "n": row["n"]
                    }
            # フォールバック: venue+surface+±200m で不足分補完
            if len(db_pace) < 3:
                rows2 = conn.execute("""
                    SELECT pace_type,
                           AVG(first_3f)     as f3,
                           AVG(last_3f_race) as l3,
                           COUNT(*)          as n
                    FROM race_laps
                    WHERE venue=? AND surface=? AND distance BETWEEN ? AND ?
                      AND pace_type IS NOT NULL
                    GROUP BY pace_type
                """, (venue, surface, distance - 200, distance + 200)).fetchall()
                for row in rows2:
                    if row["pace_type"] not in db_pace and row["n"] and row["n"] >= 5:
                        db_pace[row["pace_type"]] = {
                            "f3": row["f3"], "l3": row["l3"], "n": row["n"]
                        }
        except Exception:
            pass  # race_laps テーブルなければ無視

    # ── 2. フォールバック: 距離別ハードコード ─────────────────────
    if surface == "芝":
        dist_params = {
            1000: (32.0, 33.5),
            1200: (33.5, 33.5),
            1400: (33.8, 34.0),
            1600: (34.7, 34.1),
            1800: (36.0, 34.2),
            2000: (36.5, 34.5),
            2200: (35.0, 35.9),   # 阪神芝2200m実績ベースに修正
            2400: (37.0, 35.0),
            3000: (37.5, 35.5),
            3200: (38.0, 36.0),
        }
    else:
        dist_params = {
            1000: (32.5, 36.0),
            1200: (34.5, 36.5),
            1400: (35.5, 37.0),
            1600: (36.5, 37.5),
            1700: (37.0, 37.8),
            1800: (37.5, 38.0),
            2000: (38.5, 38.5),
            2100: (39.0, 38.8),
            2400: (40.0, 39.5),
        }

    dist_keys = sorted(dist_params.keys())
    best_key = min(dist_keys, key=lambda k: abs(k - distance))

    # ── 3. M/H/S 3Fタイム確定 (DB優先 → フォールバック) ────────────
    if "M" in db_pace:
        front3f_m = db_pace["M"]["f3"]
        last3f_m  = db_pace["M"]["l3"]
    else:
        front3f_m, last3f_m = dist_params[best_key]

    nige_count = sum(1 for h in rated_horses if h["style"] == "逃げ")
    front_runners = [h for h in rated_horses if h["style"] in ("逃げ", "先行")]

    if "H" in db_pace:
        front3f_h = db_pace["H"]["f3"]
        last3f_h  = db_pace["H"]["l3"]
    else:
        h_offset  = 1.2 if nige_count >= 2 else 0.7
        front3f_h = front3f_m - h_offset
        last3f_h  = last3f_m  + h_offset * 0.8

    if "S" in db_pace:
        front3f_s = db_pace["S"]["f3"]
        last3f_s  = db_pace["S"]["l3"]
    else:
        front3f_s = front3f_m + 1.3
        last3f_s  = last3f_m  - 1.0

    # ── 4. trigger文 ─────────────────────────────────────────────
    if nige_count >= 2:
        h_trigger = f"{nige_count}頭の逃げ馬が競り合い激流"
    elif nige_count == 1:
        h_trigger = "逃げ馬が引っ張るH流れ"
    else:
        h_trigger = "先行勢が飛ばすH流れ"

    m_trigger = f"{distance}m戦の平均的な流れ"

    front_names = [h["name"] for h in rated_horses if h["style"] == "逃げ"]
    if front_names:
        s_trigger = f"{front_names[0]}が溜める単騎逃げ"
    else:
        s_trigger = "縦長にならず上がり勝負"

    # DB使用ログ
    db_note = ""
    if db_pace:
        db_note = " [race_laps実績値]"
        print(f"  ペース3F: DB実績{db_note} "
              f"H({front3f_h:.1f}/{last3f_h:.1f}) "
              f"M({front3f_m:.1f}/{last3f_m:.1f}) "
              f"S({front3f_s:.1f}/{last3f_s:.1f})")

    scenarios = {
        "H": {
            "label": "ハイペース",
            "front3f": round(front3f_h, 1),
            "last3f":  round(last3f_h, 1),
            "trigger": h_trigger,
        },
        "M": {
            "label": "ミドルペース（典型）",
            "front3f": round(front3f_m, 1),
            "last3f":  round(last3f_m, 1),
            "trigger": m_trigger,
        },
        "S": {
            "label": "スローペース",
            "front3f": round(front3f_s, 1),
            "last3f":  round(last3f_s, 1),
            "trigger": s_trigger,
        },
    }

    # ── 5. X シナリオ: 指定馬による大逃げ ──────────────────────────
    if x_nige_name:
        x_lead     = 8   # 大逃げ馬身数
        front3f_x  = round(front3f_s + 0.7, 1)   # さらにスロー
        last3f_x   = round(last3f_s  - 0.4, 1)   # 集団の直線は速め
        scenarios["X"] = {
            "label":        f"{x_nige_name}大逃げ",
            "front3f":      front3f_x,
            "last3f":       last3f_x,
            "trigger":      f"{x_nige_name}が単独大逃げ・集団は{x_lead}馬身以上後方追走",
            "x_nige_name":  x_nige_name,
            "x_nige_lead":  x_lead,
        }
        print(f"  Xシナリオ: {x_nige_name} 大逃げ "
              f"前3F {front3f_x} / 上がり3F {last3f_x}")

    return scenarios


# ══════════════════════════════════════════════════════
# HTML生成
# ══════════════════════════════════════════════════════

HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{race_title} - ペース別展開シミュレーター</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    background:#0f1410; color:#e8eee6;
    font-family:"Hiragino Kaku Gothic ProN","Yu Gothic UI","Meiryo",sans-serif;
    padding:16px; display:flex; flex-direction:column; align-items:center;
  }}
  .wrap {{ width:100%; max-width:1000px; }}
  h1 {{ font-size:20px; letter-spacing:1px; color:#f0e6c8; }}
  h1 .gi {{ color:#7db4ff; font-size:14px; margin-left:6px; }}
  .sub {{ font-size:12px; color:#9bb09a; margin:4px 0 14px; }}
  .btns {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }}
  .pace-btn {{
    flex:1; min-width:180px; cursor:pointer; border:1px solid #2e4030;
    background:#18211a; color:#e8eee6; border-radius:10px; padding:10px 12px;
    text-align:left; transition:all .15s;
  }}
  .pace-btn:hover {{ border-color:#5a7d5e; }}
  .pace-btn.active {{ border-color:#e8c84a; background:#243024; box-shadow:0 0 12px rgba(232,200,74,.25); }}
  .pace-btn .tag {{ font-weight:bold; font-size:15px; }}
  .pace-btn .tag.H {{ color:#ff8a7a; }} .pace-btn .tag.M {{ color:#8fd18f; }} .pace-btn .tag.S {{ color:#8fb8ff; }} .pace-btn .tag.X {{ color:#d87aff; }}
  .pace-btn .f3 {{ font-size:11px; color:#b8c9b5; margin-top:3px; }}
  .pace-btn .trg {{ font-size:11px; color:#8a9a88; margin-top:2px; }}
  .replay {{
    cursor:pointer; border:1px solid #2e4030; background:#18211a; color:#e8c84a;
    border-radius:10px; padding:10px 18px; font-size:14px; font-weight:bold;
  }}
  .replay:hover {{ border-color:#e8c84a; }}
  .status {{
    display:flex; gap:18px; align-items:center; background:#18211a;
    border:1px solid #2e4030; border-radius:10px 10px 0 0; padding:8px 14px;
    font-size:13px; border-bottom:none;
  }}
  .status .clock {{ font-family:Consolas,monospace; font-size:16px; color:#f0e6c8; }}
  .status .phase {{ color:#8fd18f; font-weight:bold; }}
  .status .rem {{ color:#9bb09a; }}
  canvas {{
    width:100%; display:block; background:#13301c;
    border:1px solid #2e4030; border-radius:0 0 10px 10px;
  }}
  .strip {{
    margin-top:10px; background:#18211a; border:1px solid #2e4030; border-radius:10px;
    padding:8px 12px; font-size:12px; display:flex; gap:6px; flex-wrap:wrap; align-items:center;
    min-height:38px;
  }}
  .strip .lbl {{ color:#9bb09a; margin-right:4px; }}
  .chip {{
    display:inline-flex; align-items:center; gap:4px; background:#202b21;
    border-radius:6px; padding:2px 7px 2px 3px;
  }}
  .wk {{
    display:inline-block; width:17px; height:17px; border-radius:4px; text-align:center;
    line-height:17px; font-size:10px; font-weight:bold;
  }}
  .results {{ margin-top:12px; display:none; }}
  .results.show {{ display:block; animation:fadein .4s; }}
  @keyframes fadein {{ from{{opacity:0; transform:translateY(8px);}} to{{opacity:1; transform:none;}} }}
  .headline {{
    background:linear-gradient(90deg,#2a3a26,#18211a); border:1px solid #5a7d3e;
    border-radius:10px; padding:12px 16px; margin-bottom:10px;
  }}
  .headline .win {{ font-size:17px; font-weight:bold; color:#f0e6c8; }}
  .headline .why {{ font-size:12px; color:#b8c9b5; margin-top:5px; line-height:1.6; }}
  table {{ width:100%; border-collapse:collapse; background:#18211a; border:1px solid #2e4030; font-size:12px; }}
  th {{ background:#202b21; color:#9bb09a; font-weight:normal; padding:7px 9px; text-align:left; font-size:11px; }}
  td {{ padding:8px 9px; border-top:1px solid #242f25; vertical-align:top; line-height:1.55; }}
  td.pos {{ font-size:15px; font-weight:bold; color:#f0e6c8; white-space:nowrap; }}
  td.pos.p1 {{ color:#e8c84a; }} td.pos.p2 {{ color:#c8d4e8; }} td.pos.p3 {{ color:#d4a06a; }}
  td.horse {{ white-space:nowrap; }}
  td.horse .jk {{ color:#8a9a88; font-size:11px; }}
  td.margin {{ white-space:nowrap; color:#b8c9b5; }}
  td.reason {{ color:#c5d2c2; }}
  .note {{ font-size:11px; color:#6e7f6c; margin-top:10px; line-height:1.6; }}
  .ratings-panel {{
    margin-top:10px; background:#18211a; border:1px solid #2e4030; border-radius:10px;
    padding:8px 12px; font-size:11px; overflow-x:auto;
  }}
  .ratings-panel table {{ font-size:11px; }}
  .ratings-panel th {{ font-size:10px; padding:4px 6px; }}
  .ratings-panel td {{ padding:4px 6px; border-top:1px solid #1a2a1b; }}
  .bar-cell {{ min-width:60px; }}
  .bar {{ height:8px; border-radius:3px; display:inline-block; }}
  .bar-speed {{ background:#7db4ff; }}
  .bar-sprint {{ background:#8fd18f; }}
  .bar-stamina {{ background:#d4a06a; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{race_title}<span class="gi">{grade}</span> - ペース別展開シミュレーター</h1>
  <div class="sub">{race_sub}</div>

  <div class="btns" id="btns"></div>

  <div class="status">
    <span class="clock" id="clock">0:00.0</span>
    <span class="phase" id="phase">発走前</span>
    <span class="rem" id="rem"></span>
    <span style="margin-left:auto"><button class="replay" id="replay">▶ もう一度</button></span>
  </div>
  <canvas id="cv" width="960" height="460"></canvas>

  <div class="strip"><span class="lbl">隊列</span><span id="order"></span></div>

  <div class="results" id="results">
    <div class="headline"><div class="win" id="winLine"></div><div class="why" id="whyLine"></div></div>
    <table>
      <thead><tr><th>着順</th><th>馬</th><th>着差</th><th>なぜこの着順か</th></tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>

  <div class="ratings-panel">
    <table id="ratings-table">
      <thead>
        <tr>
          <th>馬番</th><th>馬名</th><th>騎手</th><th>脚質</th>
          <th>speed</th><th>sprint</th><th>stamina</th>
          <th>H適</th><th>M適</th><th>S適</th><th>上がり</th><th>AIスコア</th>
        </tr>
      </thead>
      <tbody id="ratings-body"></tbody>
    </table>
  </div>

  <div class="mc-panel" style="margin-top:14px; background:#18211a; border:1px solid #2e4030; border-radius:10px; padding:10px 14px;">
    <div style="font-size:13px; color:#9bb09a; margin-bottom:8px;">
      Monte Carlo 500回シミュレーション — <span id="mc-pace-label">M</span>ペース
      <span style="font-size:11px; margin-left:8px; color:#6e7f6c;">オッズとの乖離 = "ねじれ度"</span>
    </div>
    <table style="width:100%; border-collapse:collapse; font-size:12px;">
      <thead><tr>
        <th style="background:#202b21; color:#9bb09a; padding:5px 8px; text-align:left; font-weight:normal;">馬番</th>
        <th style="background:#202b21; color:#9bb09a; padding:5px 8px; text-align:left; font-weight:normal;">馬名</th>
        <th style="background:#202b21; color:#9bb09a; padding:5px 8px; text-align:left; font-weight:normal;">MC勝率</th>
        <th style="background:#202b21; color:#9bb09a; padding:5px 8px; text-align:left; font-weight:normal;">市場確率</th>
        <th style="background:#202b21; color:#9bb09a; padding:5px 8px; text-align:left; font-weight:normal;">ねじれ度</th>
      </tr></thead>
      <tbody id="mc-tbody"></tbody>
    </table>
  </div>

  <div class="note">
    ※ 本シミュレーションはkeiba.db過去成績から計算したspeed/sprint/stamina/pace適性に基づく展開モデルです。<br>
    ※ ロジック: 前半位置=脚質で決定 / 直線の伸び=sprint×pace適性÷100 / 失速チェック=stamina&lt;75×Hペース→直線3馬身失速 / Sペースは逃げ・先行に位置取りボーナス。<br>
    ※ 生成日時: {generated_at}
  </div>
</div>

<script>
"use strict";
// ============================ データ ============================
const DATA = {data_json};

// ====================== コース幾何 ======================
const TOP=95, BOT=345, LX=140, RX=820, R=100, CY=220, HW=34;
const STARTX={start_x};
const RIGHT_TURN = DATA.meta.right_turn || false;
// LA=初期直線, LB=弧, LC=縦直線 (左右共用)
const LA = STARTX - LX;   // 左回り: TOP右→左 / 右回り: BOT右→左 (どちらもSTARTX-LX)
const LB = Math.PI*R;
const LC = RX-LX;
// 右回り全周回PTOT = LA+2LB+2LC (5区間)、左回り = LA+LB+LC (3区間)
const PTOT = RIGHT_TURN ? LA+2*LB+2*LC : LA+LB+LC;
const LEN = 6;

function pos(m, o){{
  let p = Math.max(0,m)/DATA.meta.total_distance*PTOT;
  if(RIGHT_TURN){{
    // 右回り全周回 (時計回り clockwise):
    // START(STARTX,BOT) ←← [1コーナー入口(LX,BOT)] ↑↑ [2コーナー出口(LX,TOP)] →→ [3コーナー入口(RX,TOP)] ↓↓ [4コーナー出口(RX,BOT)] ←← GOAL(LX,BOT)
    if(p <= LA) return {{ x:STARTX-p, y:BOT+o }};    // 初期直線 ← (STARTX→LX)
    p -= LA;
    if(p <= LB){{
      const th = Math.PI/2 + (p/LB)*Math.PI;          // LX arc: π/2(BOT,320)→3π/2(TOP,120) 時計回り↑
      return {{ x:LX+(R+o)*Math.cos(th), y:CY+(R+o)*Math.sin(th) }};
    }}
    p -= LB;
    if(p <= LC) return {{ x:LX+p, y:TOP-o }};         // 向正面 → (LX→RX)
    p -= LC;
    if(p <= LB){{
      const th = -Math.PI/2 + (p/LB)*Math.PI;         // RX arc: -π/2(TOP,120)→π/2(BOT,320) 時計回り↓
      return {{ x:RX+(R+o)*Math.cos(th), y:CY+(R+o)*Math.sin(th) }};
    }}
    p -= LB;
    return {{ x:RX-p, y:BOT+o }};                     // 最終直線 ← GOAL(LX=140, BOT=345)
  }} else {{
    // 左回り (反時計回り): START(STARTX,TOP) → 左弧(LX)↓ → BOT右行 → GOAL(RX,BOT)
    if(p <= LA) return {{ x:STARTX-p, y:TOP-o }};
    p -= LA;
    if(p <= LB){{
      const th = -Math.PI/2 - (p/LB)*Math.PI;
      return {{ x:LX+(R+o)*Math.cos(th), y:CY+(R+o)*Math.sin(th) }};
    }}
    p -= LB;
    if(p <= LC) return {{ x:LX+p, y:BOT+o }};
    p -= LC;
    const th = Math.PI/2 - (p/(Math.PI*R))*Math.PI;
    return {{ x:RX+(R+o)*Math.cos(th), y:CY+(R+o)*Math.sin(th) }};
  }}
}}

const WAKU = {{1:["#ffffff","#222"],2:["#222222","#fff"],3:["#e8453c","#fff"],4:["#2a6fd6","#fff"],
              5:["#f2c12e","#222"],6:["#2f9e57","#fff"],7:["#ef7f24","#fff"],8:["#f0a0c0","#222"]}};

// ====================== 展開計算 ======================
const STRETCH = {{ H:1.35, M:1.0, S:0.65, X:0.55 }};
// 中盤の縦長隊列を表現するため十分な間隔を確保
const STYLE_GAP = {{ "逃げ":0, "先行":2.0, "中団":5.0, "差し":7.0, "追い込み":11.0 }};

function computeScenario(P){{
  const sc = DATA.scenarios[P];
  const effP = (P === 'X') ? 'S' : P;   // X は S ペース適性を流用
  const stretch = STRETCH[P] || 1.0;
  const xNigeNo = DATA.meta.x_nige_no || 0;
  const xLead   = (sc && sc.x_nige_lead) ? sc.x_nige_lead : 8;

  const groups = {{}};
  DATA.horses.forEach(h => (groups[h.style] = groups[h.style]||[]).push(h));
  const midMap = {{}};
  for(const stl in groups){{
    groups[stl].slice().sort((a,b)=> b.speed-a.speed || a.gate-b.gate)
      .forEach((h,i)=>{{ midMap[h.no] = (STYLE_GAP[stl] + i*0.6) * stretch; }});
  }}

  // X シナリオ: 大逃げ馬を先頭に固定、他馬を後方に引き離す
  if(P === 'X' && xNigeNo) {{
    DATA.horses.forEach(h => {{
      if(h.no === xNigeNo) {{
        midMap[h.no] = 0;                              // 先頭
      }} else {{
        midMap[h.no] = (midMap[h.no] || 0) + xLead;  // 集団は xLead 馬身後方
      }}
    }});
  }}

  // 走破タイム計算
  const _D = DATA.meta.total_distance;
  const _midFurlongs = (_D - 1200) / 200;
  const _avgFurlong  = (sc.front3f + sc.last3f) / 6;
  const mid2f = _midFurlongs * _avgFurlong;
  const T = sc.front3f + mid2f + sc.last3f;
  const t1 = 8*sc.front3f/T, t2 = t1 + 8*mid2f/T;

  const horses = DATA.horses.map(h=>{{
    const v = h.pace[effP] || 60;
    const burst = h.sprint * v / 100;
    let gain = (burst-70)*0.45 + (34.0-h.l3f)*1.5;

    // スタミナ失速判定
    const fade = (effP==="H" && h.stamina<75);
    if(fade) gain -= 3;

    let bonus = 0;
    if(effP==="S"){{ if(h.style==="逃げ") bonus=2.0; else if(h.style==="先行") bonus=0.5; }}

    // 重馬場補正: 不良>重で前有利拡大（実測: 良+10.7pt→不良+13.8pt）
    const _tc = DATA.meta.track_cond || "良";
    if(_tc === "重" || _tc === "不良") {{
      const _hv = _tc === "不良" ? 3.0 : 2.0;
      if(h.style==="逃げ") bonus += _hv;
      else if(h.style==="先行") bonus += _hv * 0.6;
      else if(h.style==="差し"||h.style==="追い込み") bonus -= _hv * 0.5;
    }}
    // コース別先行有利度補正（keiba.db 2020-2025実測値）
    const _CA = {{
      "東京": {{2600: -8}},
      "中山": {{3390: 5, 3110: 4}},
      "阪神": {{1800: 3}},
      "中京": {{1800: 3, 2000: 3}},
    }};
    const _cAdj = ((_CA[DATA.meta.venue]||{{}})[DATA.meta.total_distance])||0;
    if(_cAdj > 0 && (h.style==="逃げ"||h.style==="先行")) bonus += _cAdj * 0.5;
    if(_cAdj < 0 && (h.style==="差し"||h.style==="追い込み")) bonus += Math.abs(_cAdj) * 0.5;

    // X シナリオ固有: 大逃げ馬はスタミナ消耗で gain ペナルティ
    const isXNige = (P==='X' && h.no===xNigeNo);
    if(isXNige) {{
      gain  -= 3.5;  // 大逃げ消耗
      bonus  = 0;    // Sボーナス無効(消耗分)
    }}

    const midGap = midMap[h.no] || 0;
    const styleG0 = {{"逃げ":0,"先行":0.5,"差し":1.0,"追い込み":1.4,"中団":0.8}};

    // 外枠先行争いコスト (X では集団内競合なし)
    const _outerFront = (P !== 'X') && h.gate >= 6 && (h.style==="逃げ" || h.style==="先行");
    const _innerRivals = DATA.horses.some(r =>
      r.no !== h.no && r.gate <= 4 && (r.style==="逃げ" || r.style==="先行"));
    const congestionCost = (_outerFront && _innerRivals) ? 0.8 : 0;
    if(effP==="H" && congestionCost > 0) gain -= 1.0;

    const g0 = isXNige ? 0 : (styleG0[h.style]||1.0) + (h.gate-1)*0.06 + congestionCost;

    // 4角位置: X では大逃げ馬以外が全員差し脚質扱い(激しく詰め寄る)
    const isComer = P === 'X'
      ? (h.no !== xNigeNo)
      : (h.style==="差し"||h.style==="追い込み"||h.style==="中団");
    const q4Gap = isComer ? midGap * 0.40 : midGap * 0.90;

    return {{ h, midGap, q4Gap, g0, burst, fade, isXNige, deficit: midGap-gain-bonus }};
  }});

  const minD = Math.min(...horses.map(o=>o.deficit));
  horses.forEach(o=>{{ o.finalGap = Math.min(o.deficit-minD, 14); }});
  const order = horses.slice().sort((a,b)=>a.finalGap-b.finalGap);
  order.forEach((o,i)=> o.pos = i+1);
  makeReasons({{P, sc, horses, order, xNigeNo, xLead}});
  return {{ P, sc, T, t1, t2, horses, order }};
}}

// 変更2: gapAt に4角フェーズ(f=0.82)追加 — 5点補間
function gapAt(o, f){{
  // ks: [スタート, テン3F後, 中盤, 4角前後, ゴール]
  const ks=[0, 0.28, 0.61, 0.82, 1.0];
  const vs=[o.g0, o.midGap, o.midGap, o.q4Gap, o.finalGap];
  if(f>=1) return o.finalGap;
  let i=0; while(i<ks.length-2 && f>ks[i+1]) i++;
  const u=(f-ks[i])/(ks[i+1]-ks[i]), s=u*u*(3-2*u);
  return vs[i]+(vs[i+1]-vs[i])*s;
}}

function headDist(st, t){{
  const D = DATA.meta.total_distance;
  if(t<=st.t1) return 600*t/st.t1;
  if(t<=st.t2) return 600 + (D-1200)*(t-st.t1)/(st.t2-st.t1);
  return (D-600) + 600*(t-st.t2)/(8-st.t2);
}}

// ====================== 着差・理由 ======================
function marginLabel(d){{
  const m=[[0.1,"ハナ"],[0.2,"アタマ"],[0.35,"クビ"],[0.6,"1/2馬身"],[0.9,"3/4馬身"],[1.3,"1馬身"],
           [1.8,"1 1/2馬身"],[2.5,"2馬身"],[3.5,"3馬身"],[4.5,"4馬身"],[6,"5馬身"],[8,"7馬身"],[11,"9馬身"]];
  for(const [t,l] of m) if(d<=t) return l;
  return "大差";
}}

function makeReasons(stt){{
  const {{P, sc, xNigeNo, xLead}} = stt;
  const effP = (P==='X') ? 'S' : P;
  const topBurst = stt.horses.slice().sort((a,b)=>b.burst-a.burst)[0];
  const bestL3f  = DATA.horses.slice().sort((a,b)=>a.l3f-b.l3f)[0];
  stt.order.forEach(o=>{{
    const h=o.h, v=h.pace[effP]||60, ps=[];

    // X シナリオ専用理由文
    if(P==='X'){{
      const isXNige = (h.no===xNigeNo);
      if(isXNige){{
        ps.push(o.pos===1
          ? `大逃げをそのまま守り切り。集団を翻弄する人気薄の大金星`
          : o.pos<=4
            ? `${{xLead}}馬身の大逃げも4角で集団に飲み込まれた`
            : `大逃げの消耗で直線で力尽きた`);
      }} else {{
        ps.push(o.pos===1
          ? `大逃げ馬を差し切る末脚。集団${{xLead}}馬身後方から鮮やかに抜け出した`
          : o.pos<=3
            ? `大逃げを追って上位台頭。末脚を最後まで維持した`
            : `大逃げとの差を詰め切れず掲示板に届かず`);
        if(v>=85) ps.push(`Sペース適性${{v}}で瞬発力勝負に対応`);
      }}
    }} else if(o.fade){{
      ps.push(`前半3F ${{sc.front3f}}の激流をスタミナ${{h.stamina}}で受け切れず直線3馬身失速`);
    }} else {{
      if(v>=88)      ps.push(`ペース適性${{v}} - この流れがドンピシャ`);
      else if(v>=80) ps.push(`ペース適性${{v}}でしっかり対応`);
      else if(v>=65) ps.push(`ペース適性${{v}}でやや不向きな流れ`);
      else           ps.push(`ペース適性${{v}} - 流れが合わず伸びを欠いた`);

      if(h.style.includes("差") || h.style==="追い込み"){{
        if(P==="H")      ps.push(o.pos===1 ? `前崩れの展開を${{Math.round(o.midGap)}}馬身差から差し切り` : o.pos<=3 ? `前崩れに乗じて上位台頭` : `流れは向いたが前との差を詰め切れず`);
        else if(P==="S") ps.push(o.pos===1 ? `後方からの差し切り。sprint${{h.sprint}}の末脚がスロー瞬発力戦でフル発揮` : o.pos<=3 ? `瞬発力勝負に持ち込んだが前残りの壁をわずかに崩せず` : `後方${{Math.round(o.midGap)}}馬身のビハインドがスロー瞬発力戦でも致命的`);
        else             ps.push(o.pos<=3 ? `中団から堅実に末脚を伸ばした` : `平均ペースで前残りの壁を崩せず`);
      }} else if(h.style==="逃げ"){{
        if(P==="S")      ps.push(o.pos<=3 ? `マイペースの溜め逃げで脚を温存し直線も粘り込み` : `先頭でペースを作ったが直線で後続の瞬発力に屈した`);
        else if(P==="H") ps.push(`厳しい流れを先頭で受け切れなかった`);
        else             ps.push(o.pos<=3 ? `マイペースの逃げで上位に粘り込み` : `直線半ばで後続に交わされた`);
      }} else {{
        if(o.pos===1)      ps.push(`好位${{Math.max(2,Math.round(o.midGap))}}番手から直線で抜け出す王道の競馬`);
        else if(o.pos<=3)  ps.push(`好位追走から最後まで踏ん張った`);
        else if(v>=85)     ps.push(`流れは合ったが決め手(sprint ${{h.sprint}})の差で直線伸び負け`);
      }}
      if(h.no===topBurst.h.no && o.pos<=3) ps.push(`直線の伸び(sprint ${{h.sprint}}×適性${{v}}%=${{o.burst.toFixed(1)}})はメンバー随一`);
      if(h.no===bestL3f.no && o.pos<=3)    ps.push(`持ち上がり${{h.l3f}}はメンバー最速`);
    }}
    o.reason = ps.slice(0,3).join("。")+"。";
  }});
}}

const SUMMARY = {{
  H: s=>`${{s.trigger}} → 前半3F ${{s.front3f}}の消耗戦。先行勢が総崩れとなり、差し・追い込み勢が台頭。`,
  M: s=>`${{s.trigger}} → 前半3F ${{s.front3f}}の平均的な流れ。好位で流れに乗った馬が直線で抜け出す王道決着。`,
  S: s=>`${{s.trigger}} → 前半3F ${{s.front3f}}の超スロー。上がり${{s.last3f}}の瞬発力勝負、前にいた馬が有利。`,
  X: s=>`${{s.trigger}} → ${{s.x_nige_lead||8}}馬身の大逃げが成立。集団は超スロー追走、直線は全馬瞬発力勝負。大逃げ馬が残るか差し切られるか。`
}};

// ====================== 描画 ======================
const cv = document.getElementById("cv");
const ctx = cv.getContext("2d");
const DPR = window.devicePixelRatio || 1;
cv.width = 960*DPR; cv.height = 460*DPR;
ctx.scale(DPR, DPR);

const laneIdx = {{}};
DATA.horses.slice().sort((a,b)=> a.gate-b.gate || a.no-b.no).forEach((h,i)=> laneIdx[h.no]=i);

function drawTrack(){{
  ctx.clearRect(0,0,960,460);
  ctx.fillStyle="#13301c"; ctx.fillRect(0,0,960,460);
  const oval = new Path2D();
  oval.moveTo(RX, TOP); oval.lineTo(LX, TOP);
  oval.arc(LX, CY, R, -Math.PI/2, Math.PI/2, true);
  oval.lineTo(RX, BOT);
  oval.arc(RX, CY, R, Math.PI/2, -Math.PI/2, true);
  oval.closePath();
  ctx.lineWidth = HW*2+6; ctx.strokeStyle="#e8eee6"; ctx.stroke(oval);
  ctx.lineWidth = HW*2;   ctx.strokeStyle="#3f7d4e"; ctx.stroke(oval);
  const inn = new Path2D();
  inn.moveTo(RX, TOP+HW); inn.lineTo(LX, TOP+HW);
  inn.arc(LX, CY, R-HW, -Math.PI/2, Math.PI/2, true);
  inn.lineTo(RX, BOT-HW);
  inn.arc(RX, CY, R-HW, Math.PI/2, -Math.PI/2, true);
  inn.closePath();
  ctx.fillStyle="#2a5234"; ctx.fill(inn);
  ctx.strokeStyle="rgba(255,255,255,.25)"; ctx.lineWidth=1.5;
  for(let m=200; m<DATA.meta.total_distance; m+=200){{
    const a=pos(m,-HW+3), b=pos(m,HW-3);
    ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y); ctx.stroke();
  }}
  const s1=pos(0,-HW+2), s2=pos(0,HW-2);
  ctx.strokeStyle="#fff"; ctx.lineWidth=3;
  ctx.beginPath(); ctx.moveTo(s1.x,s1.y); ctx.lineTo(s2.x,s2.y); ctx.stroke();
  for(let i=0;i<8;i++){{
    const o1=-HW+2+(2*HW-4)*i/8, o2=-HW+2+(2*HW-4)*(i+1)/8;
    const p1=pos(DATA.meta.total_distance,o1), p2=pos(DATA.meta.total_distance,o2);
    ctx.strokeStyle = i%2 ? "#111" : "#fff"; ctx.lineWidth=6;
    ctx.beginPath(); ctx.moveTo(p1.x,p1.y); ctx.lineTo(p2.x,p2.y); ctx.stroke();
  }}
  ctx.fillStyle="#cfe0cc"; ctx.font="bold 12px sans-serif"; ctx.textAlign="center";
  if(RIGHT_TURN){{
    // 右回り全周回: START・GOAL ともにBOT上。GOAL=左端(LX)、START=右寄り(STARTX)
    ctx.fillText("GOAL", LX, BOT+HW+20);            // GOAL: BOT左端(LX=140)
    ctx.fillText("スタート", STARTX, BOT+HW+20);    // START: GOALより右
    ctx.fillStyle="#9bb09a"; ctx.font="11px sans-serif";
    ctx.textAlign="right";
    ctx.fillText("2角", LX-R-4, CY-46);             // 2コーナー出口: LX左側上部
    ctx.fillText("1角", LX-R-4, CY+52);             // 1コーナー入口: LX左側下部
    ctx.textAlign="left";
    ctx.fillText("3角", RX+R+4, CY-46);             // 3コーナー入口: RX右側上部
    ctx.fillText("4角", RX+R+4, CY+52);             // 4コーナー出口: RX右側下部
    ctx.textAlign="center";
  }} else {{
    ctx.fillText("スタート", STARTX, TOP-HW-12);    // 左回り: START on TOP
    ctx.fillText("GOAL", RX, BOT+HW+20);
    ctx.fillStyle="#9bb09a"; ctx.font="11px sans-serif";
    ctx.fillText("3角", LX-R-HW-18, CY-46);
    ctx.fillText("4角", LX-R-HW-18, CY+52);
  }}
  ctx.fillStyle="#7fa583"; ctx.font="bold 14px sans-serif";
  ctx.textAlign="center";
  ctx.fillText(DATA.meta.course, 480, 208);
  if(DATA.meta.course_variant){{
    ctx.fillStyle="#9bc89a"; ctx.font="11px sans-serif";
    ctx.fillText(DATA.meta.course_variant, 480, 224);
  }}
  // 右回り矢印 (時計回り = 右上から右下方向に回る)
  if(DATA.meta.right_turn){{
    ctx.save();
    ctx.translate(480, 240);
    ctx.strokeStyle="rgba(180,210,180,0.55)"; ctx.lineWidth=1.8;
    // 時計回り弧: 0→3π/2 (anticlockwise=false)
    ctx.beginPath(); ctx.arc(0,0,16,Math.PI*0, Math.PI*1.5, false); ctx.stroke();
    // 矢頭: 3π/2 = 下方向, 次に向かう先は 左→上 なので矢頭を左下に
    ctx.fillStyle="rgba(180,210,180,0.7)";
    ctx.beginPath(); ctx.moveTo(-16,2); ctx.lineTo(-9,-4); ctx.lineTo(-9,8); ctx.closePath(); ctx.fill();
    ctx.font="bold 9px sans-serif"; ctx.fillStyle="rgba(155,192,154,0.9)"; ctx.textAlign="center";
    ctx.fillText("右回り", 0, 32);
    ctx.restore();
  }}
  // 直線距離表示
  if(DATA.meta.straight_m){{
    ctx.fillStyle="#7fa583"; ctx.font="10px sans-serif";
    ctx.fillText("直線"+DATA.meta.straight_m+"m", 480, 345+18);
  }}
  ctx.textAlign="left";
}}

function drawHorses(st, t){{
  const head = headDist(st, t);
  const f = Math.min(head/DATA.meta.total_distance, 1);

  // 縦位置を計算（前にいる馬ほどdistMが大きい）
  const list = st.horses.map(o=>{{
    const gap = gapAt(o, f);
    return {{o, distM: Math.max(0, head - gap*LEN)}};
  }}).sort((a,b)=> b.distM - a.distM);  // 前→後ろ順

  // 動的パック割り当て: CLUSTER_DIST以内の馬を同パックに
  // 小さくして縦長の隊列を保持しつつ、本当に密集した馬だけ横に並べる
  const CLUSTER_DIST = 9;
  const assigned = new Array(list.length);
  let i = 0;
  while(i < list.length){{
    // このパックに含まれる馬の範囲[i..j]を特定
    let j = i;
    while(j < list.length-1 && list[i].distM - list[j+1].distM < CLUSTER_DIST) j++;
    const sz = j - i + 1;

    // パック内横幅: 最大HW*1.6(コース全幅)、馬数に応じてスケール
    const laneSpan = Math.min(2*(HW-3), sz * 9);
    for(let k=0; k<sz; k++){{
      const item = list[i+k];
      const h = item.o.h;
      // 基本レーン: パック内を均等配置、内枠馬を内側寄りに
      let baseLane = -laneSpan/2 + k * (laneSpan / Math.max(1, sz-1));

      // 変更4: 差し/追い込みは4角(f>0.72)から外へ進出
      if(f > 0.72 && (h.style==="差し"||h.style==="追い込み"||h.style==="中団")){{
        const e = Math.min(1, (f-0.72)/0.18);
        baseLane = baseLane + (HW-6) * 0.5 * e;
      }}
      // HW内にクリップ
      assigned[i+k] = Math.max(-(HW-3), Math.min(HW-3, baseLane));
    }}
    i = j + 1;
  }}

  // 本命馬・先頭馬の番号を特定（変更5: 名前表示を絞る）
  const honmeiNo = (DATA.horses.find(h=>h.ai_honmei)||{{}}).no;
  const frontNo  = list[0] ? list[0].o.h.no : -1;

  // 後→前の順で描画（後ろの馬を先に描いて前の馬を上に重ねる）
  [...list].reverse().forEach((item, ri)=>{{
    const idx = list.length - 1 - ri;
    const {{o, distM}} = item;
    const h = o.h;
    const lane = assigned[idx] || 0;
    const p = pos(distM, lane);
    const [bg, fg] = WAKU[h.gate]||WAKU[8];
    ctx.beginPath(); ctx.arc(p.x, p.y, 8, 0, Math.PI*2);
    ctx.fillStyle=bg; ctx.fill();
    ctx.lineWidth=1.5; ctx.strokeStyle="#1a241b"; ctx.stroke();
    ctx.fillStyle=fg; ctx.font="bold 9px sans-serif"; ctx.textAlign="center"; ctx.textBaseline="middle";
    ctx.fillText(h.no, p.x, p.y+0.5);
    // 名前は本命馬・先頭馬・先頭から3馬身以内のみ表示
    if(h.no===honmeiNo || h.no===frontNo || (list[0] && distM >= list[0].distM - 18)){{
      const ly = lane > 0 ? p.y+18 : p.y-14;
      ctx.font="10px sans-serif"; ctx.lineWidth=3;
      ctx.strokeStyle="rgba(10,18,12,.9)"; ctx.strokeText(h.name, p.x, ly);
      ctx.fillStyle=(h.no===honmeiNo ? "#e8c84a" : "#e8eee6");
      ctx.fillText(h.name, p.x, ly);
    }}
    ctx.textBaseline="alphabetic"; ctx.textAlign="left";
  }});
  return {{head, list}};
}}

// ====================== レース制御 ======================
let st=null, raf=null, t0=0, tAll=null, curP="M";

// 逃げ・先行頭数から H/M/S 確率を計算 (pace_scenario.py と同ロジック)
function calcPaceProbs(){{
  const nn = DATA.meta.n_nige || 0;
  const ns = DATA.meta.n_senkou || 0;
  let h = 0.30, s = 0.35;
  if(nn === 0){{ h = Math.max(0.05, h-0.12); s = Math.min(0.65, s+0.18); }}
  else if(nn === 1){{
    if(ns <= 2){{ s = Math.min(0.55, s+0.08); h = Math.max(0.05, h-0.05); }}
    else if(ns >= 4){{ h = Math.min(0.50, h+0.03); s = Math.max(0.10, s-0.03); }}
  }} else if(nn === 2){{ h = Math.min(0.50, h+0.08); s = Math.max(0.15, s-0.05); }}
  else {{ const adj=Math.min(0.20,0.08*(nn-1)); h=Math.min(0.55,h+adj); s=Math.max(0.10,s-adj*0.7); }}
  const m = Math.max(0.15, 1.0-h-s);
  const tot = h+m+s;
  return {{H:Math.round(h/tot*100), M:Math.round(m/tot*100), S:Math.round(s/tot*100)}};
}}
const _pp = calcPaceProbs();

const btnsEl=document.getElementById("btns");
const PACE_KEYS = Object.keys(DATA.scenarios);
for(const P of PACE_KEYS){{
  const s=DATA.scenarios[P];
  const b=document.createElement("button");
  b.className="pace-btn"; b.id="btn"+P;
  const pctStr = (P==='H'||P==='M'||P==='S') ? ` <span style="font-size:11px;opacity:.8">${{_pp[P]}}%</span>` : '';
  b.innerHTML=`<div class="tag ${{P}}">${{P}}${{pctStr}} ${{s.label}}</div>
    <div class="f3">前半3F ${{s.front3f.toFixed(1)}} / 上がり3F ${{s.last3f.toFixed(1)}}</div>
    <div class="trg">想定: ${{s.trigger}}</div>`;
  b.onclick=()=>startRace(P);
  btnsEl.appendChild(b);
}}
document.getElementById("replay").onclick=()=>startRace(curP);

function fmtClock(sec){{
  return `${{Math.floor(sec/60)}}:${{(sec%60).toFixed(1).padStart(4,"0")}}`;
}}

function startRace(P){{
  curP=P;
  document.querySelectorAll(".pace-btn").forEach(b=>b.classList.remove("active"));
  document.getElementById("btn"+P).classList.add("active");
  document.getElementById("results").classList.remove("show");
  st=computeScenario(P); tAll=null;
  if(raf) cancelAnimationFrame(raf);
  t0=performance.now();
  raf=requestAnimationFrame(frame);
  document.getElementById("mc-pace-label").textContent = P;
  setTimeout(() => buildMonteCarloTable(P), 100);
}}

function frame(now){{
  let t=(now-t0)/1000;
  if(tAll!==null && t > tAll+0.35) t = tAll+0.35;
  drawTrack();
  const {{head, list}} = drawHorses(st, t);
  document.getElementById("clock").textContent = fmtClock(Math.min(head/DATA.meta.total_distance,1)*st.T);
  const _Dtot = DATA.meta.total_distance;
  const phase = head<600 ? "テン3F" : head<(_Dtot-500) ? "中盤" : head<_Dtot ? "直線の攻防!" : "ゴール!";
  document.getElementById("phase").textContent = `${{st.sc.label}} - ${{phase}}`;
  document.getElementById("rem").textContent = head<DATA.meta.total_distance ? `残り ${{Math.ceil((DATA.meta.total_distance-head)/200)}}F` : "";
  document.getElementById("order").innerHTML = list.slice().reverse().map(({{o}})=>{{
    const [bg,fg]=WAKU[o.h.gate]||WAKU[8];
    return `<span class="chip"><span class="wk" style="background:${{bg}};color:${{fg}}">${{o.h.no}}</span>${{o.h.name}}</span>`;
  }}).join("");
  if(tAll===null && list.every(x=>x.distM>=DATA.meta.total_distance)) tAll=(now-t0)/1000;
  if(tAll!==null && (now-t0)/1000 > tAll+0.45){{
    cancelAnimationFrame(raf); raf=null;
    showResults();
    return;
  }}
  raf=requestAnimationFrame(frame);
}}

function showResults(){{
  const w=st.order[0];
  document.getElementById("winLine").textContent =
    `1着 ${{w.h.no}} ${{w.h.name}}(${{w.h.jockey}}) - ${{st.sc.label}} 走破タイム ${{fmtClock(st.T)}}`;
  document.getElementById("whyLine").textContent = SUMMARY[st.P](st.sc);
  const tb=document.getElementById("tbody"); tb.innerHTML="";
  st.order.forEach((o,i)=>{{
    const [bg,fg]=WAKU[o.h.gate]||WAKU[8];
    const margin = i===0 ? fmtClock(st.T) :
      (o.finalGap>=14 ? "大差" : marginLabel(o.finalGap - st.order[i-1].finalGap));
    const tr=document.createElement("tr");
    tr.innerHTML=`<td class="pos p${{i+1}}">${{i+1}}着</td>
      <td class="horse"><span class="wk" style="background:${{bg}};color:${{fg}}">${{o.h.no}}</span>
        ${{o.h.name}}<br><span class="jk">${{o.h.jockey}} / ${{o.h.style}}</span></td>
      <td class="margin">${{margin}}</td>
      <td class="reason">${{o.reason}}</td>`;
    tb.appendChild(tr);
  }});
  document.getElementById("results").classList.add("show");
}}

// ====================== 指数テーブル ======================
function buildRatingsTable(){{
  const tbody = document.getElementById("ratings-body");
  DATA.horses.slice().sort((a,b)=>a.no-b.no).forEach(h=>{{
    const [bg,fg]=WAKU[h.gate]||WAKU[8];
    const barW = v => Math.round((v-40)/70*60);
    const aiScore = h.ai_score || 0;
    const aiHonmei = h.ai_honmei || false;
    const aiMark = aiHonmei ? '<span style="color:#e8c84a;font-weight:bold">◎</span>' : '';
    const aiColor = aiScore >= 80 ? '#f0c050' : aiScore >= 70 ? '#8fd18f' : '#9bb09a';
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><span class="wk" style="background:${{bg}};color:${{fg}}">${{h.no}}</span></td>
      <td>${{h.name}}</td>
      <td>${{h.jockey}}</td>
      <td>${{h.style}}</td>
      <td class="bar-cell"><span style="font-size:10px">${{h.speed.toFixed(1)}}</span>
        <div><span class="bar bar-speed" style="width:${{barW(h.speed)}}px"></span></div></td>
      <td class="bar-cell"><span style="font-size:10px">${{h.sprint.toFixed(1)}}</span>
        <div><span class="bar bar-sprint" style="width:${{barW(h.sprint)}}px"></span></div></td>
      <td class="bar-cell"><span style="font-size:10px">${{h.stamina.toFixed(1)}}</span>
        <div><span class="bar bar-stamina" style="width:${{barW(h.stamina)}}px"></span></div></td>
      <td style="color:#ff8a7a">${{h.pace.H.toFixed(0)}}</td>
      <td style="color:#8fd18f">${{h.pace.M.toFixed(0)}}</td>
      <td style="color:#8fb8ff">${{h.pace.S.toFixed(0)}}</td>
      <td>${{h.l3f.toFixed(1)}}</td>
      <td style="color:${{aiColor}};font-weight:bold">${{aiMark}}${{aiScore > 0 ? aiScore.toFixed(1) : '-'}}</td>`;
    tbody.appendChild(tr);
  }});
}}

// ====================== Monte Carlo シミュレーション ======================
function runMonteCarlo(P, N=500) {{
  const sc = DATA.scenarios[P];
  const winCounts = {{}};
  DATA.horses.forEach(h => winCounts[h.no] = 0);

  for (let i = 0; i < N; i++) {{
    // 各馬にランダムノイズを加えたスコアを計算
    const noised = DATA.horses.map(h => {{
      const v = h.pace[P];
      // speed/sprint/staminaにガウスノイズ (σ=5)
      const noise = () => (Math.random() + Math.random() + Math.random() - 1.5) * 5;
      const s_speed  = h.speed  + noise();
      const s_sprint = h.sprint + noise();
      const s_stamina = h.stamina + noise() * 0.6;

      // ペース有利不利によるスタミナ補正
      const staminaFade = (P === 'H' && s_stamina < 75) ? 2 + Math.random()*2 : 0;

      // 最終パフォーマンス: sprint70%+speed30% (s_speedを適切に組み込む)
      const burst = (s_sprint * 0.7 + s_speed * 0.3) * v / 100;
      const midPenalty = P === 'H' ? (100 - v) / 8 : (P === 'S' ? (v - 80) / 10 : 0);
      const perf = burst - midPenalty - staminaFade;
      return {{ no: h.no, name: h.name, perf }};
    }});

    // パフォーマンス降順で着順確定
    noised.sort((a, b) => b.perf - a.perf);
    winCounts[noised[0].no]++;
  }}

  // 勝率に変換
  const winProb = {{}};
  DATA.horses.forEach(h => {{
    winProb[h.no] = Math.round(winCounts[h.no] / N * 100);
  }});
  return winProb;
}}

function buildMonteCarloTable(P) {{
  const winProb = runMonteCarlo(P, 1000);
  const tbody = document.getElementById("mc-tbody");
  if (!tbody) return;
  tbody.innerHTML = "";

  const sorted = DATA.horses.slice().sort((a, b) => {{
    return (winProb[b.no]||0) - (winProb[a.no]||0);
  }});

  sorted.forEach(h => {{
    const [bg,fg] = WAKU[h.gate] || WAKU[8];
    const wp = winProb[h.no] || 0;
    // 市場確率: オッズ逆数を全馬で正規化（還元率補正）
    const _oddsSum = DATA.horses.reduce((s,hh)=>s+(hh.odds?1/hh.odds:0), 0);
    const mktPct = (h.odds && _oddsSum>0) ? Math.round(100 / h.odds / _oddsSum) : 0;
    const twist = mktPct > 0 ? (wp - mktPct) : 0;
    const twistColor = twist > 3 ? '#8fd18f' : twist < -3 ? '#ff8a7a' : '#9bb09a';
    const twistStr = twist > 0 ? `+${{twist}}` : `${{twist}}`;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><span class="wk" style="background:${{bg}};color:${{fg}}">${{h.no}}</span></td>
      <td>${{h.name}}</td>
      <td style="color:#f0e6c8;font-weight:bold">${{wp}}%</td>
      <td>${{mktPct}}%</td>
      <td style="color:${{twistColor}};font-weight:bold">${{twistStr}}%</td>`;
    tbody.appendChild(tr);
  }});
}}

// ====================== 初期表示 ======================
buildRatingsTable();
drawTrack();
setTimeout(()=>startRace("M"), 500);
</script>
</body>
</html>
'''


def slugify(name: str) -> str:
    """ファイル名用スラッグ生成"""
    # 日本語はそのまま、記念・特別・GI等のスペース除去
    name = re.sub(r'[\s　]+', '_', name)
    name = re.sub(r'[^\w぀-ゟ゠-ヿ一-鿿㐀-䶿_-]', '', name)
    return name[:40]


def calc_start_x(distance: int, right_turn: bool = False) -> int:
    """距離からキャンバス上のスタート位置を計算。
    左回り: STARTX は右側 (STARTX-LX = LA)
    右回り全周回: スタートはホームストレッチ(BOT)上、GOALより手前
      阪神外回りコース1周 = 2089m。2200m → 111m手前 = STARTX=714
      L0 = (distance - circuit) / circuit * PTOT_base (≈1988px)
      STARTX = RX(820) - L0
    """
    if right_turn:
        # 右回り全周回: GOAL=LX(140), STARTX=LX+L0
        # L0 = extra_m / homestretch_m * LC_px (ホームストレッチ比例)
        CIRCUIT_M = 2089  # 阪神外回りコース1周
        HOMESTRETCH_M = 473  # 阪神外回り直線距離
        LC_PX = 680  # RX-LX
        if distance > CIRCUIT_M:
            extra_m = distance - CIRCUIT_M
            l0 = round(extra_m / HOMESTRETCH_M * LC_PX)
            return max(141, min(600, 140 + l0))  # 2200m → 140+160=300
        return 140
    base = 140 + int((distance / 1600) * 580)
    return min(820, max(300, base))


def generate_html(race_info: dict, rated_horses: list[dict], scenarios: dict,
                  output_path: str, ai_scores: dict | None = None) -> None:
    """HTMLファイルを生成する。ai_scores: {horse_name: score, '__honmei__': horse_name}"""

    # AIスコアを各馬に付与
    if ai_scores:
        honmei_name = ai_scores.get("__honmei__", "")
        for h in rated_horses:
            h["ai_score"] = ai_scores.get(h["name"], 0)
            h["ai_honmei"] = (h["name"] == honmei_name)

    race_name = race_info.get("race_name", "レース")
    venue = race_info.get("venue", "")
    surface = race_info.get("surface", "芝")
    distance = race_info.get("distance", 1600)
    track_cond = race_info.get("track_cond") or "良"
    date = race_info.get("date", "")
    furlongs = distance // 200

    surface_str = "芝" if surface == "芝" else "ダ"
    grade = ""

    race_title = f"{race_name} {date}"

    # コースバリアント (阪神Bコース等)
    course_variant = race_info.get("course_variant", "")
    right_turn = race_info.get("right_turn", False)
    straight_m = race_info.get("straight_m", None)

    # 阪神の右回りコース自動判定
    HANSHIN_RIGHT_TURN = {"阪神": True, "中山": True, "小倉": True, "函館": True, "札幌": True}
    if not right_turn and venue in HANSHIN_RIGHT_TURN:
        right_turn = True

    # 阪神外回りBコース 自動付与
    if venue == "阪神" and distance == 2200 and not course_variant:
        course_variant = "Bコース外回り 右回り"
        straight_m = straight_m or 473

    race_sub = f"{venue}{surface_str}{distance}m"
    if course_variant:
        race_sub += f" [{course_variant}]"
    race_sub += f" ({track_cond}) {furlongs}ハロン"
    if straight_m:
        race_sub += f" 直線{straight_m}m"

    # X シナリオの大逃げ馬番号を特定
    x_nige_no = 0
    if "X" in scenarios:
        x_name = scenarios["X"].get("x_nige_name", "")
        matched = next((h for h in rated_horses if h["name"] == x_name), None)
        if matched:
            x_nige_no = matched["no"]

    # DATAオブジェクト
    data_obj = {
        "meta": {
            "race": race_name,
            "course": f"{venue}{surface_str}{distance}m",
            "course_variant": course_variant,
            "right_turn": right_turn,
            "straight_m": straight_m,
            "furlongs": furlongs,
            "total_distance": distance,
            "date": date,
            "x_nige_no": x_nige_no,
            "track_cond": track_cond,
            "venue": venue,
            "n_nige": sum(1 for h in rated_horses if h.get("style") == "逃げ"),
            "n_senkou": sum(1 for h in rated_horses if h.get("style") == "先行"),
        },
        "horses": rated_horses,
        "scenarios": scenarios,
    }

    data_json = json.dumps(data_obj, ensure_ascii=False, indent=2)
    start_x = calc_start_x(distance, right_turn=right_turn)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = HTML_TEMPLATE.format(
        race_title=race_title,
        grade=grade,
        race_sub=race_sub,
        data_json=data_json,
        start_x=start_x,
        generated_at=generated_at,
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


# ══════════════════════════════════════════════════════
# --this-week: JSONからレース自動選択
# ══════════════════════════════════════════════════════

def resolve_race_id_from_json(json_race_id: str, json_race: dict) -> str:
    """
    this_week_races.jsonのrace_idをDB race_id形式に変換する。
    DB形式: '{date}_{venue}_{race_num}'
    """
    date = json_race.get("date", "")
    venue = json_race.get("venue", "")
    race_num = json_race.get("race_num", 0)
    return f"{date}_{venue}_{race_num}"


def select_best_race_from_json(json_path: str) -> tuple[str, dict] | None:
    """
    this_week_races.jsonから最もG1/G2/重賞に近いレースを選択する。
    優先順: G1 > G2 > G3 > 重賞 > 頭数多い > race_num大きい
    """
    if not os.path.exists(json_path):
        return None

    with open(json_path, encoding="utf-8") as f:
        races = json.load(f)

    if not races:
        return None

    # グレード辞書（predict_weekend.pyのGRADED_RACES相当）
    G1_NAMES = {"安田記念", "宝塚記念", "天皇賞", "ダービー", "東京優駿", "有馬記念",
                "桜花賞", "皐月賞", "菊花賞", "オークス", "優駿牝馬", "NHKマイルC",
                "マイルCS", "スプリンターズS", "高松宮記念", "大阪杯", "ジャパンC",
                "チャンピオンズC", "エリザベス女王杯", "ヴィクトリアマイル",
                "ホープフルS", "阪神JF", "朝日杯FS", "秋華賞", "フェブラリーS"}

    def score_race(race):
        rname = race.get("race_name", "")
        grade = race.get("grade", "")
        n = len(race.get("horses", []))

        if "G1" in grade or any(g1 in rname for g1 in G1_NAMES):
            return (100, n)
        elif "G2" in grade:
            return (90, n)
        elif "G3" in grade:
            return (80, n)
        elif "L" in grade or "特別" in rname or "ステークス" in rname or "S" in rname:
            return (50, n)
        else:
            return (10, n)

    best = max(races, key=score_race)
    db_race_id = resolve_race_id_from_json(best.get("race_id", ""), best)
    return db_race_id, best


# ══════════════════════════════════════════════════════
# メイン処理
# ══════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="keiba.dbからレースデータを取得して展開シミュレーターHTMLを生成する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python generate_race_sim.py --race-id 2026-06-07_東京_11
  python generate_race_sim.py --race-id 2026-06-07_東京_11 --open
  python generate_race_sim.py --this-week
        """
    )
    parser.add_argument("--race-id", type=str, default=None,
                        help="対象レースID (例: 2026-06-07_東京_11)")
    parser.add_argument("--db", type=str, default=DEFAULT_DB,
                        help=f"keiba.dbパス (デフォルト: {DEFAULT_DB})")
    parser.add_argument("--open", action="store_true",
                        help="生成後にブラウザで開く")
    parser.add_argument("--this-week", action="store_true",
                        help="this_week_races.jsonから最高グレードのレースを自動選択")
    parser.add_argument("--json-path", type=str, default="this_week_races.json",
                        help="this_week_races.jsonパス")
    parser.add_argument("--race-name", type=str, default=None,
                        help="this_week_races.jsonからレース名で選択 (例: 函館スプリントS)")
    parser.add_argument("--x-horse", type=str, default=None,
                        help="大逃げシナリオ(X)の馬名 (例: ミステリーウェイ)")

    args = parser.parse_args()

    if not args.race_id and not args.this_week and not args.race_name:
        parser.print_help()
        sys.exit(1)

    # DB接続
    if not os.path.exists(args.db):
        print(f"ERROR: {args.db} が見つかりません", file=sys.stderr)
        sys.exit(1)

    conn = get_conn(args.db)

    json_race_data = None

    # --race-name の場合: this_week_races.jsonからレース名で検索
    if args.race_name and not args.this_week:
        if not os.path.exists(args.json_path):
            print(f"ERROR: {args.json_path} が見つかりません", file=sys.stderr)
            sys.exit(1)
        with open(args.json_path, encoding="utf-8") as f:
            races = json.load(f)
        matched = [r for r in races if args.race_name in r.get("race_name", "")]
        if not matched:
            print(f"ERROR: レース名'{args.race_name}'がthis_week_races.jsonに見つかりません", file=sys.stderr)
            sys.exit(1)
        json_race_data = matched[0]
        args.race_id = resolve_race_id_from_json(json_race_data.get("race_id", ""), json_race_data)
        print(f"レース名選択: {args.race_id} ({json_race_data.get('race_name','')})")
        args.this_week = False

    # --this-week の場合
    if args.this_week:
        result = select_best_race_from_json(args.json_path)
        if result is None:
            print("ERROR: this_week_races.json が見つからないか空です", file=sys.stderr)
            sys.exit(1)
        db_race_id, json_race_data = result
        print(f"自動選択レース: {db_race_id}")
        args.race_id = db_race_id

    # レース情報取得
    race_info = get_race_info(conn, args.race_id)

    if race_info is None:
        # DBにない場合: JSONから取得
        if json_race_data is not None:
            print(f"DBにレースが見つかりません。JSONデータを使用します: {args.race_id}")
            race_info = {
                "race_id":   args.race_id,
                "date":      json_race_data.get("date", ""),
                "venue":     json_race_data.get("venue", ""),
                "race_num":  json_race_data.get("race_num", 0),
                "race_name": json_race_data.get("race_name", ""),
                "surface":   json_race_data.get("surface", "芝"),
                "distance":  json_race_data.get("distance", 1600),
                "track_cond": json_race_data.get("track_cond", "良"),
            }
        else:
            print(f"ERROR: race_id '{args.race_id}' がDBに見つかりません", file=sys.stderr)
            sys.exit(1)

    race_name = race_info["race_name"]
    surface = race_info["surface"]
    distance = race_info["distance"]
    date = race_info["date"]

    print(f"レース: {race_name} / {race_info['venue']} {surface}{distance}m / {date}")

    # Par タイム計算
    par_time = get_par_time(conn, race_info["venue"], surface, distance)
    print(f"Parタイム: {par_time:.2f}s" if par_time else "Parタイム: 計算不可 (デフォルト使用)")

    # 出走馬取得
    if json_race_data is not None:
        horses = get_race_horses_from_json(json_race_data)
    else:
        horses = get_race_horses(conn, args.race_id)

    if not horses:
        print("ERROR: 出走馬が取得できませんでした", file=sys.stderr)
        sys.exit(1)

    print(f"出走馬: {len(horses)}頭")

    # 各馬の指数計算
    rated_horses = []
    for i, horse in enumerate(horses):
        if not horse["horse_name"]:
            continue
        print(f"  計算中: {i+1}/{len(horses)} {horse['horse_name']}", end="\r")
        rated = calc_horse_ratings(conn, horse, race_info, par_time)
        rated_horses.append(rated)

    print(f"\n指数計算完了: {len(rated_horses)}頭")

    if not rated_horses:
        print("ERROR: 指数計算できた馬が0頭でした", file=sys.stderr)
        sys.exit(1)

    # X シナリオ馬の自動/手動特定
    x_nige_name = args.x_horse
    if x_nige_name is None:
        # 宝塚記念など特定G1は自動補完しない (コマンド引数で明示的に指定)
        pass

    # ペースシナリオ生成 (DB実績値 + X シナリオ)
    scenarios = estimate_scenarios(race_info, rated_horses,
                                   conn=conn, x_nige_name=x_nige_name)

    # 出力ファイル名
    race_name_slug = slugify(race_name)
    output_filename = f"race_sim_{race_name_slug}.html"
    output_path = output_filename

    # AIスコア読み込み (weekend_predictions.json から自動参照)
    ai_scores = None
    preds_path = Path(args.db).parent / "weekend_predictions.json"
    if preds_path.exists():
        try:
            with open(preds_path, encoding="utf-8") as f:
                preds = json.load(f)
            for p in preds:
                prace = p.get("race", {})
                if prace.get("race_name") == race_name or \
                   (prace.get("venue") == race_info.get("venue") and
                    prace.get("race_num") == race_info.get("race_num") and
                    prace.get("date", "").replace("-","")[:8] == (date or "").replace("-","")[:8]):
                    ai_scores = {r["horse_name"]: r["total_score"] for r in p.get("results", [])}
                    honmei = p.get("honmei", {})
                    ai_scores["__honmei__"] = honmei.get("horse_name", "")
                    print(f"AIスコア読み込み: {len(ai_scores)-1}頭 / 本命={ai_scores['__honmei__']}")
                    break
        except Exception as e:
            print(f"AIスコア読み込みエラー (無視): {e}")

    # HTML生成
    generate_html(race_info, rated_horses, scenarios, output_path, ai_scores=ai_scores)
    abs_path = os.path.abspath(output_path)
    print(f"HTML生成完了: {abs_path}")

    # ブラウザで開く
    if args.open:
        import webbrowser
        webbrowser.open(f"file:///{abs_path}")
        print("ブラウザで開きました")

    conn.close()

    # 後処理: 一時ファイル削除
    for f in ["_tmp_db_check.py", "_tmp_db_check2.py", "_tmp_db_check3.py",
              "_tmp_db_check4.py", "_tmp_db_check5.py", "_tmp_db_check6.py",
              "_tmp_db_check7.py", "_tmp_db_check8.py", "_tmp_db_check9.py",
              "_tmp_db_check10.py", "_tmp_db_check11.py"]:
        try:
            if os.path.exists(f):
                os.remove(f)
        except Exception:
            pass

    return output_path


# ══════════════════════════════════════════════════════
# C2. 脚質分類v2 — C2a-ord (2026-08-03 委員会承認・追加)
# ══════════════════════════════════════════════════════

def classify_style_c2(conn, race: dict, horses: list[dict],
                      model_path=None) -> list[dict]:
    """
    C2a-ord方式のレース単位脚質分類(classify_style()の後継候補として並存追加)。

    classify_style()との違い:
      - レース単位で全馬まとめて予測する(他馬の予測脚質構成・レース内相対ランクを
        特徴量に使うため、1頭ずつでは計算できない)
      - ロジットモデル(TRAIN 2021-2023学習・凍結)の連続「前寄り度」スコアを
        レース内で順位付けし label_simple に射影(クラス縮退が構造的に起きない)
      - 検証成績(時系列分離4ラウンド、improve_classify_style_v2〜v2d.py):
        VALID 2024-2025 acc45.26%/macro40.36%、2026HO acc45.93%/macro41.14%
        (classify_style: acc33.72%/33.88%、常時追い込み基準: 39.67%/40.01%)
      - 既存のclassify_style()は変更せず並存。呼び出し側で選択する

    引数:
      conn   : keiba.db接続(row_factory不問。履歴・種牡馬の取得に使用)
      race   : {"date","surface","distance","num_horses","track_cond","race_name"}
               (全て発走前に既知の情報のみ)
      horses : [{"horse_name","jockey", 任意:"sire","umaban","weight_kg",
                 "horse_weight"}] 出走全馬。sire省略時はDBから直近値を補完
    戻り値:
      入力と同順の [{"horse_name","style","score","rank","n_hist","fallback"}]
      style=逃げ/先行/差し/追い込み、score=連続前寄り度(0..3、小=前)、
      rank=レース内順位、fallback=履歴ゼロでprior系モデルを使ったか

    依存ファイル: classify_style_c2.py(実装本体)、classify_style_c2_model.pkl
    (build_classify_style_c2_model.pyで生成。モデル・priorの再学習頻度は未定、
    のりおさんが後日決定)。sklearn/numpy必要。
    """
    from classify_style_c2 import classify_race_c2
    return classify_race_c2(conn, race, horses, model_path=model_path)


if __name__ == "__main__":
    main()
