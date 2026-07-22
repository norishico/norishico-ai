"""
data_health_check.py — データ異常検知の基本チェック(診断レポート、fail-loudの第一歩)

背景: 2件の重大インシデント(results.oddsの広範NULL化、get_race_level_badge_infoの
NULL未処理例外による予想生成78%欠落)を踏まえ、同種の問題を早期発見するための
健全性チェック。今回はパイプラインを止める設計にはせず、診断レポート出力のみ
(hard fail-loud化は次のステップとして別途判断)。

使い方: python data_health_check.py
"""
import json
import sqlite3
from pathlib import Path
from datetime import date, timedelta

DB_PATH = 'keiba.db'


def check_odds_null_rate(conn):
    print("\n" + "=" * 70)
    print("1. results.odds の NULL率")
    print("=" * 70)
    total, null_all = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN odds IS NULL THEN 1 ELSE 0 END) FROM results"
    ).fetchone()
    print(f"  全期間: {null_all}/{total} = {null_all/total*100:.2f}%")

    today = date.today()
    for days in (7, 14, 30, 90):
        cutoff = (today - timedelta(days=days)).isoformat()
        # finish IS NOT NULL で確定済みレースのみに限定(未確定レースはodds/finishとも
        # NULLで当然なので、これを含めると「まだ終わっていないだけ」を異常と誤検知する
        # ——project既知の教訓 [[feedback_date_dayofweek]]/project_norishikoの過去の誤検知と同型)
        n, nn = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN odds IS NULL THEN 1 ELSE 0 END) "
            "FROM results WHERE date >= ? AND finish IS NOT NULL", (cutoff,)
        ).fetchone()
        rate = nn / n * 100 if n else 0
        flag = '  ⚠️要注意(10%超)' if rate > 10 else ''
        print(f"  直近{days:3d}日({cutoff}〜、確定分のみ): n={n:6d}  NULL={nn or 0:5d} ({rate:5.2f}%){flag}")

    # 日別NULL率(直近14日、finish確定分のみ=未確定レース混入によるノイズを除外)
    print("\n  日別NULL率(直近14日、finish確定分のみ):")
    cutoff14 = (today - timedelta(days=14)).isoformat()
    rows = conn.execute("""
        SELECT date, COUNT(*), SUM(CASE WHEN odds IS NULL THEN 1 ELSE 0 END)
        FROM results WHERE date >= ? AND finish IS NOT NULL
        GROUP BY date ORDER BY date
    """, (cutoff14,)).fetchall()
    for d, n, nn in rows:
        rate = (nn or 0) / n * 100 if n else 0
        flag = '  ⚠️' if rate > 5 else ''
        print(f"    {d}: n={n:4d}  NULL={nn or 0:4d} ({rate:5.2f}%){flag}")

    return {'all_time_rate_pct': round(null_all / total * 100, 3) if total else None}


def check_prediction_generation_gap():
    print("\n" + "=" * 70)
    print("2. this_week_races.json vs weekend_predictions.json のレース数差分")
    print("=" * 70)
    result = {}
    if not Path('this_week_races.json').exists():
        print("  this_week_races.json が見つかりません")
        return result
    races = json.load(open('this_week_races.json', encoding='utf-8'))
    n_races = len(races)
    print(f"  this_week_races.json: {n_races}レース")
    result['this_week_races_n'] = n_races

    if Path('weekend_predictions.json').exists():
        preds = json.load(open('weekend_predictions.json', encoding='utf-8'))
        n_preds = len(preds)
        gap = n_races - n_preds
        gap_pct = gap / n_races * 100 if n_races else 0
        flag = '  🚨要注意(20%超の生成失敗)' if gap_pct > 20 else ('  ⚠️' if gap_pct > 5 else '')
        print(f"  weekend_predictions.json: {n_preds}レース")
        print(f"  差分(生成失敗): {gap}レース ({gap_pct:.1f}%){flag}")
        result.update({'weekend_predictions_n': n_preds, 'gap': gap, 'gap_pct': round(gap_pct, 1)})
    else:
        print("  weekend_predictions.json が見つかりません(未実行、または旧ファイルなし)")
        print("  ※本日実施した反実仮想監査タスク中の検証(2026-07-22)で、現在の")
        print("    this_week_races.jsonに対しscore_weekend_race()を実行すると")
        print("    69レース中54レース(78%)がget_race_level_badge_infoのNULL例外で")
        print("    失敗することを直接確認済み(percentile_rankがNULLの行が原因、未修正)。")
        result['known_failure_from_prior_check'] = {'total': 69, 'failed': 54, 'rate_pct': 78.3}
    return result


def check_training_coverage_by_venue(conn):
    print("\n" + "=" * 70)
    print("3. 会場別 training データカバー率(直近1年、函館・札幌の薄さ再確認)")
    print("=" * 70)
    today = date.today()
    cutoff1y = (today - timedelta(days=365)).isoformat()

    # 会場別に「出走した馬×日」のうちtrainingに直近7日以内の記録がある割合
    venues = [r[0] for r in conn.execute(
        "SELECT DISTINCT venue FROM results WHERE date >= ?", (cutoff1y,)
    ).fetchall()]

    result = {}
    print(f"  {'会場':<6}{'出走数':>8}{'調教記録あり':>12}{'カバー率':>10}")
    for venue in sorted(venues):
        rows = conn.execute("""
            SELECT r.horse_name, r.date FROM results r
            WHERE r.venue=? AND r.date >= ? AND r.finish IS NOT NULL
        """, (venue, cutoff1y)).fetchall()
        n = len(rows)
        if n == 0:
            continue
        covered = 0
        for hn, d in rows:
            c = conn.execute(
                "SELECT COUNT(*) FROM training WHERE TRIM(horse_name)=TRIM(?) "
                "AND date>=DATE(?,'-7 days') AND date<?", (hn, d, d)
            ).fetchone()[0]
            if c > 0:
                covered += 1
        rate = covered / n * 100 if n else 0
        flag = '  ⚠️薄い(既知)' if rate < 30 else ''
        print(f"  {venue:<6}{n:>8}{covered:>12}{rate:>9.1f}%{flag}")
        result[venue] = {'n': n, 'covered': covered, 'rate_pct': round(rate, 1)}
    return result


def main():
    conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    conn.execute("PRAGMA cache_size=-65536")

    report = {}
    report['odds_null'] = check_odds_null_rate(conn)
    report['prediction_gap'] = check_prediction_generation_gap()
    report['training_coverage'] = check_training_coverage_by_venue(conn)

    conn.close()

    out_path = 'data_health_check_result.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n\n結果を保存: {out_path}")


if __name__ == '__main__':
    main()
