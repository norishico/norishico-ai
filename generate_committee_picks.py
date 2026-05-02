"""generate_committee_picks.py
委員会12人が独自ロジックで予想を宣言し committee_competition.json に書き込む。

各メンバーは2段階で選択する:
  1. 自分の専門基準で「注目レース」を1つ選ぶ
  2. そのレース内で自分の基準に合う馬を選ぶ
これにより全員一致を防ぎ、各メンバーの個性を発揮させる。

Usage:
  python generate_committee_picks.py                 # weekend_predictions.json の最初の日付
  python generate_committee_picks.py --date 20260427 # 指定日
  python generate_committee_picks.py --dry-run       # 書き込みなしで表示のみ
  python generate_committee_picks.py --force         # 既存エントリを上書き
"""
import argparse
import datetime as dt
import json
from pathlib import Path

ROOT = Path(__file__).parent
PRED_JSON = ROOT / "weekend_predictions.json"
COMP_JSON = ROOT / "dashboard_config" / "committee_competition.json"
JOCKEY_JSON = ROOT / "dashboard_config" / "jockey_roi.json"

# ── メンバー定義 ──────────────────────────────────────────────────────────────
MEMBERS = [
    "みなみ", "れいな", "ゆきこ", "さくら", "あかり",
    "ひなた", "あおい", "まなつ", "りこ", "かえで", "りさ", "ゆめ",
]


# ── データロード ───────────────────────────────────────────────────────────────

def load_predictions(date_str):
    """weekend_predictions.json から指定日のレースを返す。date_str: 'YYYY-MM-DD'"""
    raw = json.loads(PRED_JSON.read_text(encoding="utf-8"))
    return [r for r in raw if r["race"]["date"] == date_str]


def load_jockey_roi():
    """jockey_roi.json から {jockey: ROI} の辞書を返す (overall)"""
    if not JOCKEY_JSON.exists():
        return {}
    data = json.loads(JOCKEY_JSON.read_text(encoding="utf-8"))
    return {entry["jockey"]: entry["ROI"] for entry in data.get("overall", [])}


def _to_float(v, default=99.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def _to_int(v, default=99):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def horses_for_race(r):
    """1レースの予測dictから馬フラットリストを生成"""
    race = r["race"]
    buy_type = r.get("buy_type")
    ev7 = r.get("ev7") or 0.0
    gap = r.get("gap") or 0.0
    horse_map = {h["name"]: h for h in race.get("horses", [])}
    rows = []
    for h in r.get("results", []):
        name = h["horse_name"]
        raw = horse_map.get(name, {})
        bd = h.get("_score_breakdown") or {}
        blood_bonus = (bd.get("venue_sire", 0) + bd.get("venue_damsire", 0)
                       + bd.get("cushion_sire", 0) + bd.get("nicks", 0))
        rows.append({
            "date":         race["date"],
            "venue":        race["venue"],
            "race_num":     race["race_num"],
            "surface":      race.get("surface", ""),
            "distance":     race.get("distance", 0),
            "race_name":    race.get("race_name", ""),
            "buy_type":     buy_type,
            "ev7":          ev7,
            "gap":          gap,
            "name":         name,
            "jockey":       h.get("jockey") or raw.get("jockey", ""),
            "waku":         h.get("waku") or raw.get("waku", 0),
            "odds":         _to_float(h.get("odds") or raw.get("odds"), 99),
            "popularity":   _to_int(h.get("popularity") or raw.get("popularity"), 99),
            "total_score":  h.get("total_score") or 0.0,
            "blood_score":  h.get("_blood_score") or 0.0,
            "blood_bonus":  blood_bonus,
            "prev_pos4":    h.get("_prev_pos4") or 0,
            "running_style": h.get("_running_style") or "",
            "accel_lap":    bool(h.get("accel_lap")),
            "has_good_train": bool(h.get("has_good_train")),
            "train_count_7d": int(h.get("train_count_7d") or 0),
            "bd_nonzero":   sum(1 for v in bd.values() if v and v > 0),
        })
    return rows


def flatten_horses(races):
    """全レース×全馬のフラットリスト（fallback用）"""
    rows = []
    for r in races:
        rows.extend(horses_for_race(r))
    return rows


def make_entry(member, horse, reason, declared_at):
    return {
        "member":       member,
        "date":         horse["date"],
        "venue":        horse["venue"],
        "race_num":     horse["race_num"],
        "race_name":    horse.get("race_name", ""),
        "pick":         horse["name"],
        "pick_odds":    horse["odds"],
        "bet":          2000,
        "reason":       reason,
        "declared_at":  declared_at,
        "result_ret":   None,
        "winner":       None,
    }


# ── 各メンバーの pick ロジック（2段階: レース選択 → 馬選択）────────────────────

def pick_minami(races):
    """EV最高レース → そのレースのEV×score最大馬"""
    best_race = max(races, key=lambda r: r.get("ev7") or 0.0)
    rows = horses_for_race(best_race)
    pool = [h for h in rows if 3 <= h["odds"] <= 50] or rows
    best = max(pool, key=lambda h: h["ev7"] * h["total_score"])
    return best, f"EV最大レース選択 ev7={best['ev7']:.2f} score={best['total_score']:.1f} odds={best['odds']}倍"


def pick_reina(races):
    """1-3人気×シグナルありレース → その馬。なければ全体で低人気高スコア"""
    for r in sorted(races, key=lambda r: -(r.get("ev7") or 0)):
        rows = horses_for_race(r)
        pool = [h for h in rows if h["popularity"] <= 3 and (h["accel_lap"] or h["has_good_train"])]
        if pool:
            best = max(pool, key=lambda h: h["total_score"])
            return best, f"実際に買える馬 人気{best['popularity']}番人気 score={best['total_score']:.1f}"
    pool = [h for h in flatten_horses(races) if h["popularity"] <= 4]
    if not pool:
        pool = flatten_horses(races)
    best = max(pool, key=lambda h: h["total_score"])
    return best, f"実際に買える馬 人気{best['popularity']}番人気 score={best['total_score']:.1f}"


def pick_yukiko(races):
    """buy_typeありレース優先で最も堅い馬(最低オッズ)"""
    buy_races = [r for r in races if r.get("buy_type")]
    pool_races = buy_races if buy_races else races
    candidates = []
    for r in pool_races:
        rows = horses_for_race(r)
        pool = [h for h in rows if h["buy_type"]] or sorted(rows, key=lambda h: -h["total_score"])[:5]
        if pool:
            candidates.append(min(pool, key=lambda h: h["odds"]))
    if not candidates:
        candidates = flatten_horses(races)
    best = min(candidates, key=lambda h: h["odds"])
    return best, f"分散最小 odds={best['odds']}倍 buy={best['buy_type'] or '高スコア'}"


def pick_sakura(races):
    """血統ボーナス最大の馬がいるレース → そのレース内で血統スコア最大"""
    def race_max_blood(r):
        rows = horses_for_race(r)
        return max((h["blood_bonus"] for h in rows), default=0)
    best_race = max(races, key=race_max_blood)
    rows = horses_for_race(best_race)
    best = max(rows, key=lambda h: h["blood_score"] + h["blood_bonus"] * 10
               + (5 if h["has_good_train"] else 0))
    return best, f"血統注目レース選択 blood={best['blood_score']:.0f} bonus={best['blood_bonus']:.1f}"


def pick_akari(races):
    """市場乖離(オッズ÷期待オッズ)が最大の馬がいるレース → そのレース内で最割高"""
    def mismatch(h):
        if h["popularity"] <= 0:
            return 0.0
        expected = 16.0 / h["popularity"]
        return (h["odds"] - expected) / expected

    def race_max_mismatch(r):
        rows = horses_for_race(r)
        pool = [h for h in rows if 5 <= h["odds"] <= 80]
        return max((mismatch(h) for h in pool), default=0)

    best_race = max(races, key=race_max_mismatch)
    rows = horses_for_race(best_race)
    pool = [h for h in rows if 5 <= h["odds"] <= 80] or rows
    best = max(pool, key=mismatch)
    mm = mismatch(best)
    return best, f"市場過小評価レース選択 odds={best['odds']}倍 buy={best['buy_type'] or 'highscore'} 乖離={mm:+.1%}"


def pick_hinata(races):
    """差し・追込の展開改善ポテンシャルが最大のレース → そのレース内で展開改善馬"""
    def race_closer_potential(r):
        rows = horses_for_race(r)
        closers = [h for h in rows if "差" in h["running_style"] or "追" in h["running_style"]]
        return max((h["prev_pos4"] for h in closers), default=0)

    best_race = max(races, key=race_closer_potential)
    rows = horses_for_race(best_race)
    closers = [h for h in rows
               if ("差" in h["running_style"] or "追" in h["running_style"])
               and 3 <= h["odds"] <= 30]
    if not closers:
        closers = [h for h in rows if 3 <= h["odds"] <= 30] or rows
    best = max(closers, key=lambda h: h["prev_pos4"] * 10 + h["total_score"] * 0.1)
    return best, (f"展開改善レース選択 前走{best['prev_pos4']}番手 "
                  f"脚質={best['running_style'] or '不明'} odds={best['odds']}倍")


def pick_aoi(races, jockey_roi):
    """ROI上位騎手が乗るレース → そのレース内でjockey ROI最大"""
    def race_best_jockey_roi(r):
        rows = horses_for_race(r)
        return max((jockey_roi.get(h["jockey"], 80.0) for h in rows), default=80.0)

    best_race = max(races, key=race_best_jockey_roi)
    rows = horses_for_race(best_race)

    def jockey_score(h):
        roi = jockey_roi.get(h["jockey"], 80.0)
        return roi * 0.1 + h["total_score"] * 0.01

    best = max(rows, key=jockey_score)
    roi = jockey_roi.get(best["jockey"], 80.0)
    return best, f"騎手ROI注目レース選択 騎手={best['jockey']} ROI={roi:.0f}% score={best['total_score']:.1f}"


def pick_manatsu(races):
    """スコア最大の◎馬がいるレース → そのレース内でスコア最大"""
    def race_max_score(r):
        return max((h.get("total_score") or 0 for h in r.get("results", [])), default=0)

    best_race = max(races, key=race_max_score)
    rows = horses_for_race(best_race)
    best = max(rows, key=lambda h: h["total_score"])
    return best, f"スコア絶対最大レース選択 score={best['total_score']:.1f} buy={best['buy_type'] or 'none'}"


def pick_rico(races):
    """全指標揃い踏み(buy+accel+train+gap>=8)の馬がいるレース → その馬。フォールバック付き"""
    for r in races:
        rows = horses_for_race(r)
        pool = [h for h in rows
                if h["buy_type"] and h["accel_lap"] and h["has_good_train"] and h["gap"] >= 8]
        if pool:
            best = max(pool, key=lambda h: h["total_score"])
            flags = f"buy={bool(best['buy_type'])} accel={best['accel_lap']} train={best['has_good_train']} gap={best['gap']:.1f}"
            return best, f"全指標揃い踏み {flags}"
    for r in races:
        rows = horses_for_race(r)
        pool = [h for h in rows if h["buy_type"] and (h["accel_lap"] or h["has_good_train"])]
        if pool:
            best = max(pool, key=lambda h: h["total_score"])
            flags = f"buy={bool(best['buy_type'])} accel={best['accel_lap']} train={best['has_good_train']} gap={best['gap']:.1f}"
            return best, f"全指標揃い踏み {flags}"
    all_rows = flatten_horses(races)
    pool = [h for h in all_rows if h["buy_type"]] or all_rows
    best = max(pool, key=lambda h: h["total_score"])
    flags = f"buy={bool(best['buy_type'])} accel={best['accel_lap']} train={best['has_good_train']} gap={best['gap']:.1f}"
    return best, f"全指標揃い踏み {flags}"


def pick_kaede(races):
    """市場確率乖離が最大の馬がいるレース → そのレース内で最乖離馬"""
    def mismatch(h):
        if h["popularity"] <= 0:
            return 0.0
        expected = 16.0 / h["popularity"]
        return (h["odds"] - expected) / expected

    def race_max_mismatch(r):
        rows = horses_for_race(r)
        pool = [h for h in rows if 5 <= h["odds"] <= 40]
        return max((mismatch(h) for h in pool), default=0)

    best_race = max(races, key=race_max_mismatch)
    rows = horses_for_race(best_race)
    pool = [h for h in rows if 5 <= h["odds"] <= 40] or rows
    best = max(pool, key=mismatch)
    mm = mismatch(best)
    return best, f"市場確率ずれレース選択 odds={best['odds']}倍 人気{best['popularity']}番 乖離={mm:+.1%}"


def pick_risa(races):
    """データ完全性(bd_nonzero×train)最大の馬がいるレース → そのレース内で完全性最大"""
    def race_completeness(r):
        rows = horses_for_race(r)
        return max((h["bd_nonzero"] * 10 + h["train_count_7d"] for h in rows), default=0)

    best_race = max(races, key=race_completeness)
    rows = horses_for_race(best_race)
    best = max(rows, key=lambda h: h["bd_nonzero"] * 10 + h["train_count_7d"] + h["total_score"] * 0.01)
    return best, f"データ完全性最大レース選択 bd_nonzero={best['bd_nonzero']} train7d={best['train_count_7d']}"


def pick_yume(races):
    """好調教馬が最も多いレース → そのレース内で好調教×非逃げ×調教本数最大"""
    def race_good_train_count(r):
        rows = horses_for_race(r)
        return sum(1 for h in rows if h["has_good_train"])

    best_race = max(races, key=race_good_train_count)
    rows = horses_for_race(best_race)
    pool = [h for h in rows
            if h["has_good_train"] and h["train_count_7d"] >= 3
            and "逃" not in h["running_style"]]
    if not pool:
        pool = [h for h in rows if h["has_good_train"]] or rows
    best = max(pool, key=lambda h: h["train_count_7d"] * 5 + h["total_score"] * 0.1)
    return best, (f"好調教レース選択 train={best['train_count_7d']}本/7日 "
                  f"脚質={best['running_style'] or '不明'} score={best['total_score']:.1f}")


# ── メイン ───────────────────────────────────────────────────────────────────

def generate_picks(date_str, force=False):
    races = load_predictions(date_str)
    if not races:
        print(f"weekend_predictions.json に {date_str} のデータなし")
        return []

    if not any(r.get("results") for r in races):
        print("有効な馬データなし")
        return []

    jockey_roi = load_jockey_roi()

    comp = json.loads(COMP_JSON.read_text(encoding="utf-8"))
    existing_members = set(
        e["member"] for e in comp.get("entries", [])
        if e.get("date") == date_str
    )

    declared_at = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    pick_funcs = {
        "みなみ": lambda: pick_minami(races),
        "れいな": lambda: pick_reina(races),
        "ゆきこ": lambda: pick_yukiko(races),
        "さくら": lambda: pick_sakura(races),
        "あかり": lambda: pick_akari(races),
        "ひなた": lambda: pick_hinata(races),
        "あおい": lambda: pick_aoi(races, jockey_roi),
        "まなつ": lambda: pick_manatsu(races),
        "りこ":   lambda: pick_rico(races),
        "かえで": lambda: pick_kaede(races),
        "りさ":   lambda: pick_risa(races),
        "ゆめ":   lambda: pick_yume(races),
    }

    new_entries = []
    print(f"\n=== 委員会予想宣言 {date_str} ===")
    print(f"{'メンバー':<8} {'会場':>4} {'R':>3} {'馬名':<16} {'オッズ':>6} {'人気':>4}  理由")
    print("─" * 80)

    for member in MEMBERS:
        if member in existing_members and not force:
            print(f"{member:<8}  (スキップ: 宣言済み)")
            continue
        try:
            horse, reason = pick_funcs[member]()
        except Exception as e:
            print(f"{member:<8}  (エラー: {e})")
            continue

        entry = make_entry(member, horse, reason, declared_at)
        new_entries.append(entry)
        print(f"{member:<8} {horse['venue']:>4} {horse['race_num']:>3}R "
              f"{horse['name']:<16} {horse['odds']:>5.1f}倍 {horse['popularity']:>3}人  {reason}")

    return new_entries, comp


def _write_entries(new_entries, comp, dry_run, force):
    """new_entriesをcomp_jsonに書き込む共通処理"""
    if not new_entries:
        print("\n追加エントリなし")
        return
    if dry_run:
        print(f"\n[DRY-RUN] {len(new_entries)}件 (書き込みなし)")
        return
    if force:
        members_to_replace = {e["member"] for e in new_entries}
        date_to_replace = new_entries[0]["date"]
        comp["entries"] = [
            e for e in comp.get("entries", [])
            if not (e.get("date") == date_to_replace and e.get("member") in members_to_replace)
        ]
    comp.setdefault("entries", []).extend(new_entries)
    COMP_JSON.write_text(json.dumps(comp, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ {len(new_entries)}件 → {COMP_JSON.name} に書き込み完了")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYYMMDD (default: 最初の日付)")
    ap.add_argument("--all", action="store_true", dest="all_dates",
                    help="weekend_predictions.json の全日付を処理")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="既存エントリを上書き")
    args = ap.parse_args()

    raw = json.loads(PRED_JSON.read_text(encoding="utf-8"))
    available = sorted(set(r["race"]["date"] for r in raw))

    if args.date:
        target_dates = [dt.datetime.strptime(args.date, "%Y%m%d").strftime("%Y-%m-%d")]
    elif args.all_dates:
        target_dates = available
        print(f"全日付処理: {target_dates}")
    else:
        target_dates = [available[0]] if available else [dt.date.today().strftime("%Y-%m-%d")]
        print(f"日付自動選択: {target_dates[0]} (利用可能: {available})")

    for date_str in target_dates:
        print(f"\n--- {date_str} ---")
        result = generate_picks(date_str, force=args.force)
        if not result:
            continue
        new_entries, comp = result
        _write_entries(new_entries, comp, args.dry_run, args.force)


if __name__ == "__main__":
    main()
