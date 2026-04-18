"""毎レース発走前にオッズ更新 → 買い判定再チェック → 変更あればデプロイ

軽量モード: オッズだけ取得 → EV/買い判定再計算 → 変更あればHTML再生成+push
フルモード: fetch→predict→HTML→push（朝1回 or --full指定時）

Usage:
  python auto_refresh.py              # 全レース発走10分前に軽量チェック
  python auto_refresh.py --minutes 15 # 15分前にトリガー
  python auto_refresh.py --dry-run    # スケジュール確認のみ
  python auto_refresh.py --full       # フル更新モード（従来動作）
"""

import json, time, subprocess, sys, argparse, re, shutil
from datetime import datetime, timedelta
from pathlib import Path

PROJ_DIR = Path(__file__).parent
PYEXE = shutil.which('py') or sys.executable


def load_all_race_schedule(minutes_before=10):
    """全レースの発走時刻からスケジュールを生成"""
    data = json.load(open(PROJ_DIR / 'weekend_predictions.json', encoding='utf-8'))
    today = datetime.now().strftime('%Y-%m-%d')

    triggers = []
    for p in data:
        r = p['race']
        stime = r.get('start_time', '')
        if not stime:
            continue
        try:
            race_dt = datetime.strptime(f"{today} {stime}", '%Y-%m-%d %H:%M')
        except:
            continue

        trigger_dt = race_dt - timedelta(minutes=minutes_before)
        venue = r.get('venue', '')
        rnum = r.get('race_num', 0)
        is_buy = bool(p.get('buy_type') or p.get('special_horse'))

        triggers.append({
            'trigger': trigger_dt,
            'race_time': race_dt,
            'label': f"{venue}{rnum}R",
            'is_buy': is_buy,
        })

    triggers.sort(key=lambda x: x['trigger'])

    # 近い時刻（3分以内）をまとめる
    merged = []
    for t in triggers:
        if merged and (t['trigger'] - merged[-1]['trigger']).total_seconds() < 180:
            merged[-1]['races'].append(t['label'])
            if t['is_buy']:
                merged[-1]['has_buy'] = True
        else:
            merged.append({
                'trigger': t['trigger'],
                'races': [t['label']],
                'has_buy': t['is_buy'],
            })

    return merged


def calc_market_momentum():
    """予想時の◎オッズ vs 現在オッズを比較し、市場動向(momentum)を計算

    momentum = (initial_odds - current_odds) / initial_odds
      > +0.20: 🔥 市場強力支持 (オッズ20%以上下落 = 買われている)
      > +0.10: ↑ 支持上昇
      < -0.20: ⚠️ 支持低下 (オッズ20%以上上昇 = 嫌われている)
      else:    → 安定

    結果を weekend_predictions.json の各レースに 'momentum' フィールドとして保存
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By

    pred_path = PROJ_DIR / 'weekend_predictions.json'
    races_path = PROJ_DIR / 'this_week_races.json'
    if not pred_path.exists() or not races_path.exists():
        print("  ⏭ 予想ファイル未生成、momentum計算スキップ")
        return

    preds = json.load(open(pred_path, encoding='utf-8'))
    races = json.load(open(races_path, encoding='utf-8'))
    race_map = {r['race_id']: r for r in races}

    today = datetime.now().strftime('%Y-%m-%d')

    # Selenium で当日レースのオッズだけ取得
    opts = Options()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--lang=ja')
    opts.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
    d = webdriver.Chrome(options=opts)

    momentum_count = 0
    for p in preds:
        rid = p['race']['race_id']
        honmei = p.get('honmei', {})
        initial_odds = honmei.get('odds', 0) or 0
        if initial_odds <= 0:
            continue

        # 発走済みスキップ
        stime = p['race'].get('start_time', '')
        if stime:
            try:
                race_dt = datetime.strptime(f"{today} {stime}", '%Y-%m-%d %H:%M')
                if datetime.now() > race_dt:
                    continue
            except Exception:
                pass

        # 現在オッズ取得
        try:
            url = f"https://race.netkeiba.com/race/shutuba.html?race_id={rid}"
            d.get(url)
            time.sleep(2)
            rows = d.find_elements(By.CSS_SELECTOR, 'table.Shutuba_Table tr.HorseList')
            race_data = race_map.get(rid, {})
            horses = race_data.get('horses', [])

            # honmei の馬名で現在オッズを見つける
            honmei_name = honmei.get('horse_name', '')
            current_odds = 0
            for row, horse in zip(rows, horses):
                if horse.get('name', '').strip() == honmei_name.strip():
                    try:
                        import re
                        pop_td = row.find_element(By.CSS_SELECTOR, 'td.Popular')
                        odds_match = re.search(r'[\d.]+', pop_td.text.strip())
                        if odds_match:
                            current_odds = float(odds_match.group())
                    except Exception:
                        pass
                    break

            if current_odds <= 0:
                continue

            # momentum計算
            momentum = (initial_odds - current_odds) / initial_odds
            if momentum > 0.20:
                label = '🔥強力支持'
            elif momentum > 0.10:
                label = '↑支持上昇'
            elif momentum < -0.20:
                label = '⚠️支持低下'
            else:
                label = '→安定'

            p['momentum'] = {
                'initial_odds': initial_odds,
                'current_odds': current_odds,
                'change_pct': round(momentum * 100, 1),
                'label': label,
            }
            momentum_count += 1
            print(f"  📈 {p['race'].get('venue','')}{p['race'].get('race_num','')}R "
                  f"◎{honmei_name} {initial_odds}→{current_odds} ({momentum*100:+.1f}%) {label}")
        except Exception as e:
            pass

    d.quit()

    # 保存
    with open(pred_path, 'w', encoding='utf-8') as f:
        json.dump(preds, f, ensure_ascii=False, indent=2)
    print(f"  📊 momentum計算: {momentum_count}レース更新")


def quick_odds_refresh(morning_mode=False):
    """軽量: オッズだけ再取得 → 買い判定チェック → 変更あればHTML再生成+push"""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By

    print(f"  ⚡ オッズ軽量チェック {datetime.now().strftime('%H:%M:%S')}")

    # 現在の予想を読み込み
    preds = json.load(open(PROJ_DIR / 'weekend_predictions.json', encoding='utf-8'))
    races_json = json.load(open(PROJ_DIR / 'this_week_races.json', encoding='utf-8'))
    race_map = {r['race_id']: r for r in races_json}

    # 現在の買いレースを記録（◎オッズ+馬場状態+special_horseフル情報も保持）
    # v6.6改善: special_horse の完全な辞書を保存してロック復元に使えるように
    old_buys = {}
    old_cond = {}
    for p in preds:
        rid = p['race']['race_id']
        bt = p.get('buy_type', '')
        sp = p.get('special_horse')
        if bt or sp:
            honmei_odds = p.get('honmei', {}).get('odds', 0) or 0
            old_buys[rid] = {
                'type': bt or 'special',
                'odds': honmei_odds,
                'buy_type': bt,  # 元のbuy_type('v6_normal'等)を保存
                'special_horse': sp,  # C2/F1の完全情報（Noneの場合もある）
            }
        old_cond[rid] = p['race'].get('track_cond', '良') or '良'

    # Seleniumでオッズだけ取得
    opts = Options()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--lang=ja')
    opts.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
    d = webdriver.Chrome(options=opts)

    updated = 0
    skipped = 0
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    for race in races_json:
        rid = race['race_id']
        # 発走済みレースはスキップ
        stime = race.get('start_time', '')
        if stime:
            try:
                race_dt = datetime.strptime(f"{today_str} {stime}", '%Y-%m-%d %H:%M')
                if now > race_dt:
                    skipped += 1
                    continue
            except:
                pass
        url = f"https://race.netkeiba.com/race/shutuba.html?race_id={rid}"
        d.get(url)
        time.sleep(2)

        try:
            # 馬場状態
            try:
                data_el = d.find_element(By.CSS_SELECTOR, '.RaceData01')
                data_text = data_el.text.strip()
                m = re.search(r'馬場[：:]?\s*(良|稍重?|重|不良)', data_text)
                if m:
                    c = m.group(1)
                    race['track_cond'] = '稍重' if c == '稍' else c
            except:
                pass

            # オッズ更新
            rows = d.find_elements(By.CSS_SELECTOR, 'table.Shutuba_Table tr.HorseList')
            for row, horse in zip(rows, race.get('horses', [])):
                try:
                    pop_td = row.find_element(By.CSS_SELECTOR, 'td.Popular')
                    odds_text = pop_td.text.strip()
                    odds_match = re.search(r'[\d.]+', odds_text)
                    if odds_match:
                        horse['odds'] = odds_match.group()
                except:
                    pass
            updated += 1
        except:
            pass

    d.quit()
    print(f"  📊 {updated}R のオッズ更新完了（発走済み{skipped}Rスキップ）")

    # JSON保存（prevも保存）
    shutil.copy2(
        PROJ_DIR / 'weekend_predictions.json',
        PROJ_DIR / 'weekend_predictions_prev.json'
    )
    with open(PROJ_DIR / 'this_week_races.json', 'w', encoding='utf-8') as f:
        json.dump(races_json, f, ensure_ascii=False, indent=2)

    # 再スコアリング → 買い判定
    print(f"  🔄 再スコアリング...")
    result = subprocess.run(
        [PYEXE, '-X', 'utf8', str(PROJ_DIR / 'predict_weekend.py')],
        cwd=str(PROJ_DIR), capture_output=True, text=True, encoding='utf-8'
    )

    # 新しい買いレースを確認
    new_preds = json.load(open(PROJ_DIR / 'weekend_predictions.json', encoding='utf-8'))

    # ±20%ロック: 朝値(morning_snapshot)基準で±20%以内かつ馬場変更なしなら復元
    # (2026-04-18 委員会決定: 直前値基準だと累積ズレで朝から大乖離でもロック維持される欠陥があった)
    morning_snapshot_for_lock = {}
    snap_path = PROJ_DIR / 'morning_snapshot.json'
    if snap_path.exists():
        try:
            morning_snapshot_for_lock = json.load(open(snap_path, encoding='utf-8'))
        except Exception:
            pass

    new_buys_raw = {}
    new_cond = {}
    for p in new_preds:
        rid = p['race']['race_id']
        bt = p.get('buy_type', '')
        sp = p.get('special_horse')
        new_cond[rid] = p['race'].get('track_cond', '良') or '良'
        if bt or sp:
            new_buys_raw[rid] = {'type': bt or 'special', 'odds': p.get('honmei', {}).get('odds', 0) or 0}

    locked = 0
    for rid, old_info in old_buys.items():
        if rid in new_buys_raw:
            continue  # まだ買い判定→変更なし
        # 旧買いが消えた→ロック判定
        # 基準値: 朝 snapshot にあれば朝値、なければ old_info(途中追加レース用)
        morning_info = morning_snapshot_for_lock.get(rid, {})
        ref_odds = morning_info.get('honmei_odds') or old_info['odds']
        ref_cond = morning_info.get('track_cond') or old_cond.get(rid, '良')
        # 新しい◎のオッズを取得
        new_honmei_odds = 0
        for p in new_preds:
            if p['race']['race_id'] == rid:
                new_honmei_odds = p.get('honmei', {}).get('odds', 0) or 0
                break
        # 馬場変更チェック(朝基準)
        cond_changed = ref_cond != new_cond.get(rid, '良')
        # ±20%以内(朝値基準) かつ 馬場変更なし → 除外をブロック（買い判定を復元）
        if ref_odds > 0 and new_honmei_odds > 0 and not cond_changed:
            ratio = new_honmei_odds / ref_odds
            if 0.8 <= ratio <= 1.2:
                # 元の買い判定を復元（v6.6: special_horseも正しく復元）
                for p in new_preds:
                    if p['race']['race_id'] == rid:
                        if old_info.get('buy_type'):
                            p['buy_type'] = old_info['buy_type']
                        if old_info.get('special_horse'):
                            p['special_horse'] = old_info['special_horse']
                        p['pass_reason'] = ''  # lock復元時にpass_reasonクリア
                        p['_locked'] = True    # ロック維持フラグ (HTML表示用)
                        break
                locked += 1

    if locked:
        try:
            from data_source import get_lock_source_of_truth
            _sot = get_lock_source_of_truth()
        except Exception:
            _sot = "netkeiba"
        print(f"  🔒 {locked}R の買い判定をロック維持（オッズ±20%以内 / source_of_truth={_sot}）")
        # ロック復元した予想を保存
        with open(PROJ_DIR / 'weekend_predictions.json', 'w', encoding='utf-8') as f:
            json.dump(new_preds, f, ensure_ascii=False, indent=2)

    # 発走前最終チェックのタイムスタンプ設定 (→ HTML「✅ 買いGO」表示)
    _now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    for p in new_preds:
        bt = p.get('buy_type', '')
        sp = p.get('special_horse')
        if bt or sp:
            p['_last_odds_check'] = _now_str

    # 更新を保存 (ロック有無に関わらず最終チェック記録)
    with open(PROJ_DIR / 'weekend_predictions.json', 'w', encoding='utf-8') as f:
        json.dump(new_preds, f, ensure_ascii=False, indent=2)

    # 変更検出 → アラートログに蓄積
    from alerts_log import compare_and_log
    new_alerts = compare_and_log(preds, new_preds)
    changes = [a['text'] for a in new_alerts]

    # 通知ロジック (差分ベース)
    # morning_mode=True の --once 時は notify_morning_summary 側で担当するのでスキップ
    if not morning_mode:
        # 朝スナップショットをロード (追加通知判定用)
        morning_snapshot = {}
        snap_path = PROJ_DIR / 'morning_snapshot.json'
        if snap_path.exists():
            try:
                morning_snapshot = json.load(open(snap_path, encoding='utf-8'))
            except Exception:
                pass

        try:
            from scripts.notify import notify_buy_go, notify_cancelled, notify_added
            for p in new_preds:
                rid = p['race']['race_id']
                bt = p.get('buy_type', '')
                sp = p.get('special_horse')
                is_target = bool(bt or sp)
                was_morning = rid in morning_snapshot
                was_last = rid in old_buys
                lc = p.get('_last_odds_check', '')

                if is_target and not was_last and not was_morning:
                    # 朝にも前回にもなかった → 新規対象入り
                    notify_added(p)
                elif not is_target and was_last:
                    # 前回まで対象 → 今回外れた
                    notify_cancelled(p, p.get('pass_reason', 'オッズ変動'))
                elif is_target and lc:
                    # 発走10分前の最終確定
                    notify_buy_go(p)
        except Exception as ne:
            print(f"  📧 通知エラー: {ne}")

    if changes:
        print(f"  🚨 変更あり!")
        for c in changes:
            print(f"    {c}")
        # HTML再生成+デプロイ
        subprocess.run(
            [PYEXE, '-X', 'utf8', str(PROJ_DIR / 'generate_weekend_prediction.py')],
            cwd=str(PROJ_DIR), capture_output=True
        )
        docs = PROJ_DIR / 'docs'
        docs.mkdir(exist_ok=True)
        shutil.copy2(PROJ_DIR / 'this_week_prediction.html', docs / 'index.html')
        subprocess.run(['git', 'add', 'docs/index.html'], cwd=str(PROJ_DIR))
        now = datetime.now().strftime('%H:%M')
        subprocess.run(
            ['git', 'commit', '-m', f'Auto: odds update {now} - {", ".join(changes)}'],
            cwd=str(PROJ_DIR)
        )
        subprocess.run(['git', 'push'], cwd=str(PROJ_DIR))
        print(f"  ✅ デプロイ完了")
    else:
        print(f"  ✅ 変更なし（デプロイ省略）")

    return bool(changes)


def full_refresh(day_flag='--saturday'):
    """フル更新: publish_weekend.py --refresh-odds"""
    print(f"\n{'='*50}")
    print(f"🔄 フル更新 {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*50}")
    result = subprocess.run(
        [PYEXE, '-X', 'utf8', str(PROJ_DIR / 'publish_weekend.py'),
         day_flag, '--refresh-odds'],
        cwd=str(PROJ_DIR), capture_output=False,
    )
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description='NORISHICO KEIBA AI 自動更新')
    parser.add_argument('--minutes', type=int, default=10, help='発走何分前に更新（デフォルト10）')
    parser.add_argument('--dry-run', action='store_true', help='スケジュール確認のみ')
    parser.add_argument('--full', action='store_true', help='フル更新モード')
    parser.add_argument('--sunday', action='store_true', help='日曜モード')
    parser.add_argument('--monday', action='store_true', help='月曜モード')
    parser.add_argument('--once', action='store_true',
                        help='1回だけ強制オッズチェック+再判定して終了（朝の初回スナップショット用）')
    args = parser.parse_args()

    # --once: 1回だけ強制チェックして終了 + momentum計算 + 朝サマリ通知 + snapshot保存
    if args.once:
        print(f"🐴 NORISHICO KEIBA AI 強制1回チェック（{datetime.now().strftime('%H:%M:%S')}）")
        try:
            calc_market_momentum()
            changed = quick_odds_refresh(morning_mode=True)
            print(f"✅ 強制チェック完了 changed={changed}")
            # 朝スナップショット保存 + 朝サマリ通知
            try:
                preds = json.load(open(PROJ_DIR / 'weekend_predictions.json', encoding='utf-8'))
                morning_targets = {
                    p['race']['race_id']: {
                        'venue': p['race'].get('venue', ''),
                        'race_num': p['race'].get('race_num', 0),
                        'race_name': p['race'].get('race_name', ''),
                        'buy_type': p.get('buy_type'),
                        'special_horse': bool(p.get('special_horse')),
                        # ±20%ロック判定の基準値(朝の honmei オッズ+馬場)
                        'honmei_odds': (p.get('honmei') or {}).get('odds', 0) or 0,
                        'track_cond': p['race'].get('track_cond', '良') or '良',
                    }
                    for p in preds if p.get('buy_type') or p.get('special_horse')
                }
                with open(PROJ_DIR / 'morning_snapshot.json', 'w', encoding='utf-8') as f:
                    json.dump(morning_targets, f, ensure_ascii=False, indent=2)
                print(f"  💾 morning_snapshot.json 保存 ({len(morning_targets)}件)")
                from scripts.notify import notify_morning_summary
                notify_morning_summary(preds)
                print("  📧 朝サマリ通知 sent")
            except Exception as ne:
                print(f"  📧 朝通知エラー: {ne}")
        except Exception as e:
            import traceback
            print(f"❌ 強制チェックエラー: {e}")
            traceback.print_exc()
        return

    mode = 'フル' if args.full else '軽量オッズ'
    print(f"🐴 NORISHICO KEIBA AI 自動更新（{mode}モード）")
    print(f"   全レース発走{args.minutes}分前にチェック")
    print(f"   現在時刻: {datetime.now().strftime('%H:%M:%S')}")
    print()

    schedule = load_all_race_schedule(args.minutes)

    if not schedule:
        print("⚠️ レースが見つかりません")
        return

    # スケジュール表示
    print(f"📋 更新スケジュール（{len(schedule)}回）")
    print(f"{'─'*50}")
    now = datetime.now()
    for i, s in enumerate(schedule, 1):
        status = '⏳' if s['trigger'] > now else '⏭ 済'
        races = ' / '.join(s['races'])
        buy_mark = ' ★' if s['has_buy'] else ''
        print(f"  {i:>2}. {s['trigger'].strftime('%H:%M')} → {races}{buy_mark} {status}")
    print(f"{'─'*50}")
    print(f"  ★ = 現時点で期待値ありレースを含む")
    print()

    if args.dry_run:
        print("(dry-run: ここで終了)")
        return

    # 待機ループ
    if args.monday:
        day_flag = '--monday'
    elif args.sunday:
        day_flag = '--sunday'
    else:
        day_flag = '--saturday'
    executed = set()
    # ヘルスチェック用: 毎時最新タイムスタンプをファイルに記録
    health_file = PROJ_DIR / 'auto_refresh_health.txt'
    last_health_hour = -1
    while True:
        now = datetime.now()
        next_trigger = None
        # ヘルスチェック: 時間が変わるごとに記録
        if now.hour != last_health_hour:
            last_health_hour = now.hour
            try:
                with open(health_file, 'w', encoding='utf-8') as hf:
                    hf.write(f'alive {now.strftime("%Y-%m-%d %H:%M:%S")}\n')
                    hf.write(f'executed {len(executed)}/{len(schedule)}\n')
            except Exception:
                pass

        for i, s in enumerate(schedule):
            if i in executed:
                continue
            if now >= s['trigger']:
                races = ' / '.join(s['races'])
                print(f"\n⏰ {s['trigger'].strftime('%H:%M')} {races}")
                if args.full:
                    full_refresh(day_flag)
                else:
                    quick_odds_refresh()
                executed.add(i)
            elif next_trigger is None:
                next_trigger = s

        if len(executed) == len(schedule):
            print(f"\n🏁 全{len(schedule)}回の更新完了")
            # ── 結果自動保存 ──
            # 全レース発走済み後、netkeibaから結果取得→月間JSON追記→HTML再生成
            print(f"\n📥 本日の結果を取得・保存開始 {datetime.now().strftime('%H:%M:%S')}")
            today_date = datetime.now().strftime('%Y%m%d')
            try:
                result = subprocess.run(
                    [PYEXE, '-X', 'utf8', str(PROJ_DIR / 'publish_weekend.py'),
                     '--save-results', today_date],
                    cwd=str(PROJ_DIR), capture_output=True, text=True, encoding='utf-8',
                    timeout=1800,  # 30分タイムアウト
                )
                if result.returncode == 0:
                    print(f"  ✅ 結果保存完了（{today_date}分をHTML結果タブに反映）")
                else:
                    print(f"  ⚠️ 結果保存失敗 (rc={result.returncode})")
                    if result.stderr:
                        print(f"  stderr: {result.stderr[-500:]}")
            except subprocess.TimeoutExpired:
                print(f"  ⚠️ 結果保存タイムアウト（30分超過）")
            except Exception as e:
                print(f"  ⚠️ 結果保存エラー: {e}")
            break

        if next_trigger:
            wait = (next_trigger['trigger'] - now).total_seconds()
            if wait > 0:
                races = ' / '.join(next_trigger['races'])
                print(f"\r⏳ 次: {next_trigger['trigger'].strftime('%H:%M')} ({races})"
                      f" あと{int(wait//60)}分{int(wait%60)}秒  ", end='', flush=True)
                time.sleep(min(wait, 30))


if __name__ == '__main__':
    main()
