"""nar_daily.py - 南関東競馬 毎日の自動実行パイプライン
  1. 昨日の結果を取得 (DB更新)
  2. 今日の出馬表を取得
  3. スコアリング
  4. HTML生成
  5. Discord通知

使い方:
  python nar/nar_daily.py                     # 本日分
  python nar/nar_daily.py --date 2026-04-26   # 指定日
  python nar/nar_daily.py --no-discord        # 通知なし
"""
import sys, json, os, requests, argparse, subprocess
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')

PROJ = Path(__file__).parent.parent
NAR_DIR = Path(__file__).parent
load_dotenv(PROJ / '.env')

DISCORD_WEBHOOK = os.environ.get('DISCORD_WEBHOOK_URL', '')
# エラー専用webhook (未設定なら通常webhookにフォールバック)
DISCORD_ERROR_WEBHOOK = os.environ.get('DISCORD_ERROR_WEBHOOK_URL') or DISCORD_WEBHOOK

VENUE_EMOJI = {'浦和': '🔵', '船橋': '🟢', '大井': '🔴', '川崎': '🟠'}


def send_discord(content: str, embeds=None, error=False):
    webhook = DISCORD_ERROR_WEBHOOK if error else DISCORD_WEBHOOK
    if not webhook:
        print("  [Discord] Webhook未設定")
        return
    payload = {'content': content}
    if embeds:
        payload['embeds'] = embeds
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        r.raise_for_status()
        print(f"  [Discord] 送信OK ({r.status_code})")
    except Exception as e:
        print(f"  [Discord] 送信エラー: {e}", file=sys.stderr)


def notify_nar_predictions(races_scored, target_date):
    buy_races = [r for r in races_scored if r.get('honmei')]
    if not buy_races:
        send_discord(f"🏇 **NAR予想 {target_date}**\n本日の南関東買いレース: なし\n全レースパス")
        return

    lines = [f"🏇 **NORISHICO NAR予想 {target_date}**", f"買いレース: {len(buy_races)}R", ""]
    for item in buy_races:
        race = item['race']
        h = item['honmei']
        venue = race.get('venue_name', '')
        em = VENUE_EMOJI.get(venue, '🏟')
        odds = h.get('odds')
        score = h.get('score', 0)
        odds_str = f"{odds:.1f}倍" if odds else "?"
        lines.append(
            f"{em} **{venue}{race['race_num']}R** {race.get('class_code','')} {race.get('distance','')}m\n"
            f"  ◎ {h['umaban']}番 **{h['horse_name']}** ({h.get('jockey','')})\n"
            f"  単勝 {odds_str} ／ スコア {score:.1f}"
        )

    send_discord('\n'.join(lines))


def notify_nar_results(results_summary, target_date):
    n = results_summary['n']
    wins = results_summary['wins']
    roi = results_summary['roi']
    cost = results_summary['cost']
    ret = results_summary['ret']

    sign = '+' if ret - cost >= 0 else ''
    lines = [
        f"📊 **NAR結果 {target_date}**",
        f"買い: {n}R / 的中: {wins}R ({wins/n*100:.0f}%)" if n else "買い: 0R",
        f"収支: {sign}{ret-cost:,}円 (ROI {roi:.0f}%)" if n else "",
    ]
    send_discord('\n'.join(l for l in lines if l))


def save_predictions(races_scored, target_date):
    """予想内容をJSONに保存 (後日結果照合用)"""
    pred_dir = PROJ / 'nar_predictions'
    pred_dir.mkdir(exist_ok=True)
    out = pred_dir / f'nar_pred_{target_date.replace("-","")}.json'

    records = []
    for item in races_scored:
        race = item['race']
        honmei = item.get('honmei')
        if not honmei:
            continue
        records.append({
            'date': target_date,
            'race_id': race.get('race_id', ''),
            'venue': race.get('venue_name', ''),
            'race_num': race.get('race_num'),
            'class_code': race.get('class_code', ''),
            'distance': race.get('distance'),
            'condition': race.get('condition', ''),
            'umaban': honmei.get('umaban'),
            'horse_name': honmei.get('horse_name', ''),
            'jockey': honmei.get('jockey', ''),
            'odds': honmei.get('odds'),
            'score': honmei.get('score'),
            'finish': None,  # 後日更新
            'result_odds': None,
        })

    with open(out, 'w', encoding='utf-8') as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"  予想保存: {out} ({len(records)}R)")
    return records


def update_db_for_date(date_str):
    """指定日の結果をDBに取り込む"""
    script = NAR_DIR / 'build_nar_db.py'
    d_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}" if len(date_str) == 8 else date_str
    result = subprocess.run(
        [sys.executable, str(script), '--date', d_fmt],
        capture_output=True, text=True, encoding='utf-8', cwd=str(PROJ)
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
    print(result.stdout.strip())


def update_prediction_results(date_str):
    """前日の予想ファイルに実際の結果を書き込む"""
    pred_dir = PROJ / 'nar_predictions'
    pred_file = pred_dir / f'nar_pred_{date_str.replace("-","")}.json'
    if not pred_file.exists():
        return None

    import sqlite3
    conn = sqlite3.connect(PROJ / 'nar_keiba.db')

    with open(pred_file, encoding='utf-8') as f:
        preds = json.load(f)

    updated = 0
    cost = ret = 0
    wins = 0
    for p in preds:
        if p.get('finish') is not None:
            continue
        race_id = p.get('race_id', '')
        umaban = p.get('umaban')
        if not race_id or umaban is None:
            continue
        row = conn.execute(
            "SELECT finish, odds FROM nar_results WHERE race_id=? AND umaban=?",
            (race_id, umaban)
        ).fetchone()
        if row:
            p['finish'] = row[0]
            p['result_odds'] = row[1]
            updated += 1
            cost += 1000
            if row[0] == 1:
                ret += int((row[1] or 0) * 100) * 10
                wins += 1
            else:
                ret += 0

    conn.close()

    if updated > 0:
        with open(pred_file, 'w', encoding='utf-8') as f:
            json.dump(preds, f, ensure_ascii=False, indent=2)
        roi = ret / cost * 100 if cost else 0
        print(f"  予想結果更新: {updated}R 勝{wins} ROI{roi:.0f}% 収支{ret-cost:+,}円")
        return {'date': date_str, 'n': updated, 'wins': wins, 'cost': cost, 'ret': ret, 'roi': roi}
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', help='YYYY-MM-DD (default: today)')
    parser.add_argument('--no-discord', action='store_true')
    parser.add_argument('--days', type=int, default=1)
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else date.today()
    target_str = target.isoformat()

    print(f"=== NAR daily {target_str} ===")

    # 1. 昨日分の結果取得 (DBアップデート)
    yesterday_date = (target - timedelta(days=1))
    yesterday = yesterday_date.strftime('%Y%m%d')
    yesterday_iso = yesterday_date.isoformat()
    print(f"\n[1] 昨日({yesterday})の結果取得...")
    update_db_for_date(yesterday)

    # 1b. 昨日の予想結果照合
    res = update_prediction_results(yesterday_iso)
    if res and not args.no_discord:
        notify_nar_results(res, yesterday_iso)

    # [H-NAR-1] ペース免罪仮説 日次観測ログ(実弾投入なし・紙上検証専用、2026-09〜)
    # v3.3本体とは完全に独立した処理。ここで例外が起きてもv3.3側の処理は継続する
    # (逆にv3.3がこの後失敗してもH-NAR-1側の記録は既に完了している)。
    try:
        sys.path.insert(0, str(NAR_DIR))
        from hnar1_daily_log import run as run_hnar1_daily_log
        hnar1_result = run_hnar1_daily_log(target_str)
        print(f"\n[H-NAR-1] {target_str}: status={hnar1_result.get('status')} "
              f"該当{len(hnar1_result.get('picks', []))}頭 "
              f"(races_checked={hnar1_result.get('races_checked')}) ※実弾投入なし・記録のみ")
    except Exception as e:
        print(f"\n[H-NAR-1] エラー(v3.3側には影響なし): {e}", file=sys.stderr)

    # 2. 今日の出馬表取得
    print(f"\n[2] 本日({target_str})の出馬表取得...")
    fetch_script = NAR_DIR / 'fetch_nar_shutsuba.py'
    result = subprocess.run(
        [sys.executable, str(fetch_script), '--date', target_str, '--days', str(args.days)],
        capture_output=True, text=True, encoding='utf-8', cwd=str(PROJ)
    )
    print(result.stdout.strip())
    if result.returncode != 0:
        err_msg = (result.stderr or '').strip() or '(no stderr)'
        print(err_msg, file=sys.stderr)
        if not args.no_discord:
            send_discord(f"🚨 **NAR スクレイプ失敗** {target_str}\n```{err_msg[:500]}```", error=True)
        return

    shutsuba_path = PROJ / 'nar_shutsuba.json'
    if not shutsuba_path.exists():
        print("出馬表ファイルなし。終了")
        if not args.no_discord:
            send_discord(f"🚨 **NAR 出馬表未生成** {target_str}\nnar_shutsuba.json が存在しません", error=True)
        return

    with open(shutsuba_path, encoding='utf-8') as f:
        all_races = json.load(f)

    if not all_races:
        print("対象レースなし")
        return

    # 3. スコアリング
    print(f"\n[3] スコアリング ({len(all_races)}R)...")
    sys.path.insert(0, str(NAR_DIR))
    from scoring_nar import score_race, pick_honmei, get_db

    conn = get_db()
    races_scored = []
    for race in all_races:
        scored = score_race(race, conn)
        honmei, reason = pick_honmei(scored, race_class_code=race.get('class_code', ''),
                                     venue_name=race.get('venue_name', ''))
        races_scored.append({
            'race': race,
            'scored_entries': scored,
            'honmei': honmei,
            'pass_reason': reason if not honmei else '',
        })
    conn.close()

    buy_count = sum(1 for r in races_scored if r['honmei'])
    print(f"  買い: {buy_count}R / パス: {len(races_scored)-buy_count}R")

    # 4. HTML生成
    print(f"\n[4] HTML生成...")
    from predict_nar import build_html
    html = build_html(races_scored, target_str)
    out_path = PROJ / f'nar_prediction_{target_str.replace("-","")}.html'
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"  HTML: {out_path}")

    # 4b. 予想保存
    save_predictions(races_scored, target_str)

    # 5. Discord通知
    if not args.no_discord:
        print(f"\n[5] Discord通知...")
        notify_nar_predictions(races_scored, target_str)

    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))
        from generate_project_status import generate as _gen_ps
        _gen_ps('nar_daily')
    except Exception:
        pass

    print(f"\n=== 完了 ===")
    return races_scored


if __name__ == '__main__':
    main()
