"""期待値ありレースの発走15分前に自動でオッズ更新+再デプロイ

Usage:
  python auto_refresh.py             # 予想データから自動スケジュール
  python auto_refresh.py --minutes 20  # 20分前にトリガー（デフォルト15分）
  python auto_refresh.py --dry-run   # 実行せずスケジュール確認のみ
"""

import json, time, subprocess, sys, argparse
from datetime import datetime, timedelta
from pathlib import Path

PROJ_DIR = Path(__file__).parent
PYEXE = sys.executable


def load_schedule(minutes_before=15):
    """weekend_predictions.jsonから期待値ありレースの発走時刻を取得し、
    更新タイミングを計算"""
    data = json.load(open(PROJ_DIR / 'weekend_predictions.json', encoding='utf-8'))
    today = datetime.now().strftime('%Y-%m-%d')

    triggers = []
    for p in data:
        bt = p.get('buy_type', '')
        sp = p.get('special_horse')
        if not bt and not sp:
            continue

        r = p['race']
        stime = r.get('start_time', '')
        if not stime:
            continue

        # 発走時刻をdatetimeに
        try:
            race_dt = datetime.strptime(f"{today} {stime}", '%Y-%m-%d %H:%M')
        except:
            continue

        trigger_dt = race_dt - timedelta(minutes=minutes_before)
        venue = r.get('venue', '')
        rnum = r.get('race_num', 0)
        rname = r.get('race_name', '')

        triggers.append({
            'trigger': trigger_dt,
            'race_time': race_dt,
            'label': f"{venue}{rnum}R {rname}",
            'start_time': stime,
        })

    # 時刻順にソート
    triggers.sort(key=lambda x: x['trigger'])

    # 近い時刻（5分以内）のトリガーをまとめる
    merged = []
    for t in triggers:
        if merged and (t['trigger'] - merged[-1]['trigger']).total_seconds() < 300:
            merged[-1]['races'].append(t['label'])
            # 早い方の時刻を採用
            if t['trigger'] < merged[-1]['trigger']:
                merged[-1]['trigger'] = t['trigger']
        else:
            merged.append({
                'trigger': t['trigger'],
                'races': [t['label']],
            })

    return merged


def run_refresh():
    """オッズ再取得→予想→HTML→デプロイ"""
    print(f"\n{'='*50}")
    print(f"🔄 オッズ更新開始 {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*50}")

    result = subprocess.run(
        [PYEXE, '-X', 'utf8', str(PROJ_DIR / 'publish_weekend.py'),
         '--saturday', '--refresh-odds'],
        cwd=str(PROJ_DIR),
        capture_output=False,
    )

    if result.returncode == 0:
        print(f"✅ 更新完了 {datetime.now().strftime('%H:%M:%S')}")
    else:
        print(f"❌ エラー（returncode={result.returncode}）")

    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description='期待値レース発走前 自動更���')
    parser.add_argument('--minutes', type=int, default=15, help='発走何分前に更新（デフォルト15）')
    parser.add_argument('--dry-run', action='store_true', help='スケジュール確認のみ')
    args = parser.parse_args()

    print(f"🐴 NORISHICO AI 自動更新モード")
    print(f"   発走{args.minutes}分前にオッズ更新+再デプ��イ")
    print(f"   現在時刻: {datetime.now().strftime('%H:%M:%S')}")
    print()

    schedule = load_schedule(args.minutes)

    if not schedule:
        print("⚠️ 期待値ありレースが見つかりません")
        return

    # スケジュール表示
    print(f"📋 更新スケジュール（{len(schedule)}回）")
    print(f"{'─'*50}")
    now = datetime.now()
    for i, s in enumerate(schedule, 1):
        status = '⏳' if s['trigger'] > now else '⏭ 済'
        races = ' / '.join(s['races'])
        print(f"  {i}. {s['trigger'].strftime('%H:%M')} → {races} {status}")
    print(f"{'─'*50}")
    print()

    if args.dry_run:
        print("(dry-run: ここで終了)")
        return

    # 待機ループ
    executed = set()
    while True:
        now = datetime.now()
        next_trigger = None

        for i, s in enumerate(schedule):
            if i in executed:
                continue
            if now >= s['trigger']:
                # トリガー実行
                races = ' / '.join(s['races'])
                print(f"\n⏰ トリガー発動: {races}")
                run_refresh()
                executed.add(i)
            elif next_trigger is None:
                next_trigger = s

        # 全トリガー実行済み
        if len(executed) == len(schedule):
            print(f"\n🏁 全{len(schedule)}回の更新が完了しま���た")
            break

        # 次のトリガーまで待機
        if next_trigger:
            wait = (next_trigger['trigger'] - now).total_seconds()
            if wait > 0:
                races = ' / '.join(next_trigger['races'])
                print(f"\r⏳ 次の更新: {next_trigger['trigger'].strftime('%H:%M')}（{races}）"
                      f" あと{int(wait//60)}分{int(wait%60)}秒  ", end='', flush=True)
                time.sleep(min(wait, 30))  # 30秒ごとに表示更新


if __name__ == '__main__':
    main()
