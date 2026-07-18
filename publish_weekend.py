"""週末予想の統合パイプライン
fetch → predict → HTML生成 → Note記事生成 → GitHub Pages デプロイ

Usage:
  python publish_weekend.py                    # 今週土日フル実行
  python publish_weekend.py --saturday         # 土曜のみ
  python publish_weekend.py --sunday           # 日曜のみ
  python publish_weekend.py --refresh-odds     # オッズ再取得+HTML再生成のみ（スコア再計算あり）
  python publish_weekend.py --skip-fetch       # fetch省略（JSONが既にある前提）
  python publish_weekend.py --no-deploy        # デプロイ省略
"""

import sys, os, time, json, argparse, subprocess, shutil
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PROJ_DIR = Path(__file__).parent
DOCS_DIR = PROJ_DIR / 'docs'
PYEXE = sys.executable


def get_weekend_dates():
    """今週の土日日付を返す（YYYYMMDD形式）"""
    today = datetime.now()
    wd = today.weekday()  # 0=Mon, 5=Sat, 6=Sun
    if wd <= 4:  # Mon-Fri → 次の土曜
        sat = today + timedelta(days=(5 - wd))
    elif wd == 5:  # Sat
        sat = today
    else:  # Sun
        sat = today - timedelta(days=1)
    sun = sat + timedelta(days=1)
    return sat.strftime('%Y%m%d'), sun.strftime('%Y%m%d')


def get_day_label(date_str):
    """YYYYMMDD → '4/5(土)' 形式"""
    dt = datetime.strptime(date_str, '%Y%m%d')
    weekdays = ['月', '火', '水', '木', '金', '土', '日']
    return f"{dt.month}/{dt.day}({weekdays[dt.weekday()]})"


def _odds_valid_rate(races):
    """取得済みレースのオッズ有効率を計算（netkeiba切替時間帯のマスク検知用）"""
    total = 0
    valid = 0
    for r in races:
        for h in r.get('horses', []):
            total += 1
            o = str(h.get('odds', '')).strip()
            try:
                if o and float(o) > 0:
                    valid += 1
            except ValueError:
                pass
    return (valid / total) if total else 0.0


def step_fetch(dates, proj_dir, _retry=0):
    """Step 1: netkeibaから出走表+オッズを取得
    netkeibaが19時前後に想定→前日オッズを切り替える時間帯では odds が '**' マスクに
    なることがある。有効率<50%なら120秒待って1回だけretry、それでもダメなら例外。
    """
    print("\n" + "="*60)
    print(f"STEP 1: netkeibaからデータ取得 {'(retry)' if _retry else ''}")
    print("="*60)

    from fetch_shutsuba import create_driver, quit_driver, fetch_race_list, fetch_shutsuba

    driver = create_driver()
    all_races = []
    try:
        for date_str in dates:
            print(f"\n📅 {get_day_label(date_str)} のレース取得中...")
            races = fetch_race_list(driver, date_str)
            time.sleep(0.5)
            for race in races:
                race_data = fetch_shutsuba(driver, race['race_id'])
                race_data['date'] = datetime.strptime(date_str, '%Y%m%d').strftime('%Y-%m-%d')
                all_races.append(race_data)
                time.sleep(0.5)
    finally:
        quit_driver(driver)

    # race.netkeiba.com が 0 件（IP ブロック等）→ sp.netkeiba.com フォールバック
    if not all_races:
        print("\n[fallback] race.netkeiba.com から 0 件 → sp.netkeiba.com フォールバックを試みます")
        try:
            from fetch_shutsuba import create_driver as _cd, quit_driver as _qd
            import fetch_shutsuba_sp as _sp
            _drv = _cd()
            try:
                all_races = _sp.fetch_this_week_races_sp(_drv)
            finally:
                _qd(_drv)
            print(f"[fallback] sp フォールバック成功: {len(all_races)} 件")
        except Exception as _e:
            print(f"[fallback] sp フォールバックも失敗: {_e}")

    rate = _odds_valid_rate(all_races)
    print(f"\n  オッズ有効率: {rate*100:.1f}%")
    if rate < 0.5:
        if _retry == 0:
            print("  ⚠️  オッズ欠損率が高い(切替時間帯の可能性)。120秒待ってretryします")
            time.sleep(120)
            return step_fetch(dates, proj_dir, _retry=1)
        # Discord障害通知発射(Webhook失敗は許容、最低限の情報共有)
        try:
            sys.path.insert(0, str(proj_dir))
            from scripts.notify import _send
            _send(
                f"⚠️ **予想生成失敗**\n\n"
                f"netkeibaからのオッズ取得に失敗しました(有効率 {rate*100:.1f}%)。\n"
                f"切替時間帯の影響か、netkeiba側の障害の可能性があります。\n"
                f"時間を置いて再実行してください。"
            )
        except Exception as _ne:
            print(f"  📧 障害通知送信エラー: {_ne}")
        raise RuntimeError(
            f"オッズ取得に失敗 (有効率{rate*100:.1f}%)。netkeibaが "
            "オッズ切替時間帯(金曜19時台など)に当たった可能性。時間を置いて再実行してください。"
        )

    outfile = proj_dir / 'this_week_races.json'
    with open(outfile, 'w', encoding='utf-8') as f:
        json.dump(all_races, f, ensure_ascii=False, indent=2)

    print(f"\n✅ {len(all_races)}レース取得 → {outfile.name}")
    return all_races


def step_predict(proj_dir):
    """Step 2: スコアリング+買い判定"""
    print("\n" + "="*60)
    print("STEP 2: スコアリング実行")
    print("="*60)

    result = subprocess.run(
        [PYEXE, '-X', 'utf8', str(proj_dir / 'predict_weekend.py')],
        cwd=str(proj_dir), capture_output=True, text=True, encoding='utf-8'
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"❌ Error:\n{result.stderr}")
        return False
    return True


def step_save_prev(proj_dir):
    """予想JSONをprevとして保存（アラート差分検出用）"""
    src = proj_dir / 'weekend_predictions.json'
    dst = proj_dir / 'weekend_predictions_prev.json'
    if src.exists():
        shutil.copy2(src, dst)


def step_detect_alerts(proj_dir):
    """予想の差分を検出してアラートログに追記"""
    sys.path.insert(0, str(proj_dir))
    from alerts_log import compare_and_log
    prev_path = proj_dir / 'weekend_predictions_prev.json'
    curr_path = proj_dir / 'weekend_predictions.json'
    if not prev_path.exists() or not curr_path.exists():
        return []
    prev = json.load(open(prev_path, encoding='utf-8'))
    curr = json.load(open(curr_path, encoding='utf-8'))
    return compare_and_log(prev, curr)


def step_fetch_results(date_str, proj_dir):
    """Step R: レース結果をnetkeibaから取得"""
    print("\n" + "="*60)
    print(f"STEP R: {date_str} の結果取得")
    print("="*60)

    import re, tempfile, shutil as _shutil
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By

    _tmpdir_results = tempfile.mkdtemp(prefix='chrome_results_')
    opts = Options()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--lang=ja')
    opts.add_argument(f'--user-data-dir={_tmpdir_results}')
    opts.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
    d = webdriver.Chrome(options=opts)

    # レースID取得
    from fetch_shutsuba import fetch_race_list
    races = fetch_race_list(d, date_str)
    import time as _time
    _time.sleep(2)

    all_results = []
    for race in races:
        rid = race['race_id']
        url = f'https://race.netkeiba.com/race/result.html?race_id={rid}'
        d.get(url)
        _time.sleep(3)

        info = {'race_id': rid, 'race_num': int(rid[-2:]), 'venue': '', 'race_name': '', 'track_cond': '', 'horses': []}
        try:
            try: info['race_name'] = d.find_element(By.CSS_SELECTOR, '.RaceName').text.strip()
            except: pass
            try:
                d2 = d.find_element(By.CSS_SELECTOR, '.RaceData02').text.strip()
                for v in ['札幌','函館','福島','新潟','東京','中山','中京','京都','阪神','小倉']:
                    if v in d2: info['venue'] = v; break
            except: pass
            try:
                d1 = d.find_element(By.CSS_SELECTOR, '.RaceData01').text.strip()
                m = re.search(r'馬場[：:]?\s*(良|稍重?|重|不良)', d1)
                if m:
                    c = m.group(1)
                    info['track_cond'] = '稍重' if c == '稍' else c
            except: pass

            rows = d.find_elements(By.CSS_SELECTOR, 'table.RaceTable01 tr')
            for row in rows[1:]:
                tds = row.find_elements(By.TAG_NAME, 'td')
                if len(tds) < 10: continue
                ft = tds[0].text.strip()
                if not ft.isdigit(): continue
                umaban = tds[2].text.strip()
                try: name = row.find_element(By.CSS_SELECTOR, '.Horse_Name a, span.Horse_Name').text.strip()
                except: name = tds[3].text.strip().split('\n')[0]
                try: pop = tds[9].text.strip()
                except: pop = ''
                try: odds = tds[10].text.strip()
                except: odds = ''
                info['horses'].append({'finish': int(ft), 'umaban': umaban, 'name': name, 'odds': odds, 'popularity': pop})
        except Exception as e:
            print(f'  Error {rid}: {e}')

        w = info['horses'][0]['name'] if info['horses'] else '?'
        print(f'  {info["venue"]}{info["race_num"]:>2}R {info["race_name"]:>15} {info.get("track_cond","?")} 1着:{w}')
        all_results.append(info)

    d.quit()
    _shutil.rmtree(_tmpdir_results, ignore_errors=True)

    outfile = proj_dir / f'results_{date_str}.json'
    with open(outfile, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f'  ✅ {len(all_results)}R → {outfile.name}')
    return outfile


def step_save_monthly(results_file, preds_file, proj_dir):
    """Step M: 結果を月間JSONに保存"""
    print("\n" + "="*60)
    print("STEP M: 月間結果に保存")
    print("="*60)

    result = subprocess.run(
        [PYEXE, '-X', 'utf8', str(proj_dir / 'save_results.py'),
         str(results_file), str(preds_file)],
        cwd=str(proj_dir), capture_output=True, text=True, encoding='utf-8'
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"❌ Error:\n{result.stderr}")
        return False
    return True


def step_html(proj_dir):
    """Step 3: 予想HTML生成"""
    print("\n" + "="*60)
    print("STEP 3: HTML生成")
    print("="*60)

    result = subprocess.run(
        [PYEXE, '-X', 'utf8', str(proj_dir / 'generate_weekend_prediction.py')],
        cwd=str(proj_dir), capture_output=True, text=True, encoding='utf-8'
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"❌ Error:\n{result.stderr}")
        return False
    return True


def step_note(proj_dir):
    """Step 4: Note記事+Xポスト生成"""
    print("\n" + "="*60)
    print("STEP 4: Note記事 + Xポスト生成")
    print("="*60)

    result = subprocess.run(
        [PYEXE, '-X', 'utf8', str(proj_dir / 'generate_note_article.py')],
        cwd=str(proj_dir), capture_output=True, text=True, encoding='utf-8'
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"❌ Error:\n{result.stderr}")
        return False
    return True


SPREADSHEET_WEBHOOK = 'https://script.google.com/macros/s/AKfycbzRxGGdFZ3oUwsEG6tUTxSlP1b2HUF5n5NfKpZ-XGF1PW2_JRc-G1vzpwFvXnX38jn2cQ/exec'

BUY_TYPE_LABELS = {
    'v6_star3': '単勝◎+馬連◎○',
    'v6_star2': '単勝◎+馬連◎○',
    'v6_challenge': '単勝◎のみ',
    'special': '単勝のみ（別枠）',
}


def step_post_spreadsheet(proj_dir):
    """Step S: 結果をGoogleスプレッドシートにPOST"""
    print("\n" + "="*60)
    print("STEP S: スプレッドシートに反映")
    print("="*60)

    import urllib.request

    monthly_files = sorted(Path(proj_dir).glob('monthly_results_*.json'))
    if not monthly_files:
        print("  ⚠️ 月間結果ファイルがありません")
        return False

    monthly = json.load(open(monthly_files[-1], encoding='utf-8'))
    if not monthly.get('days'):
        print("  ⚠️ 結果データがありません")
        return False

    # 最新日の結果をPOST
    latest_day = monthly['days'][-1]
    rows = []
    for br in latest_day.get('buy_results', []):
        bt_raw = br.get('buy_type', '')
        bt_label = BUY_TYPE_LABELS.get(bt_raw, bt_raw)
        rows.append({
            'date': latest_day['date'],
            'venue': br.get('venue', ''),
            'race_num': br.get('race_num', 0),
            'race_name': br.get('race_name', ''),
            'grade': br.get('grade', ''),
            'buy_type': bt_label,
            'honmei': br.get('honmei', ''),
            'honmei_finish': br.get('honmei_finish', '?'),
            'honmei_odds': br.get('honmei_odds', 0),
            'ni': br.get('ni', ''),
            'ni_finish': br.get('ni_finish', '?'),
            'winner': br.get('winner', ''),
            'winner_odds': br.get('winner_odds', ''),
            'track_cond': br.get('track_cond', ''),
            'cost': br.get('cost', 0),
            'ret': br.get('return', 0),
            'profit': br.get('profit', 0),
        })

    if not rows:
        print("  ⚠️ POSTする結果がありません")
        return False

    payload = json.dumps({'rows': rows}).encode('utf-8')
    req = urllib.request.Request(
        SPREADSHEET_WEBHOOK,
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode('utf-8')
            print(f"  ✅ {len(rows)}行をスプシに送信: {body}")
            return True
    except Exception as e:
        print(f"  ❌ スプシ送信エラー: {e}")
        return False


def step_deploy(proj_dir):
    """Step 5: docs/ にコピーして git push（GitHub Pages用）"""
    print("\n" + "="*60)
    print("STEP 5: GitHub Pages デプロイ")
    print("="*60)

    # gitリポジトリ確認
    git_dir = proj_dir / '.git'
    if not git_dir.exists():
        print("  ⚠️ Gitリポジトリが未初期化。手動で以下を実行してください:")
        print(f"    cd {proj_dir}")
        print("    git init")
        print("    git remote add origin <your-repo-url>")
        print("    git add docs/index.html")
        print("    git commit -m 'Update weekend prediction'")
        print("    git push origin main")
        print("\n  GitHub Settings → Pages → Source: 'main' branch, '/docs' folder")
        return False

    # git add + commit + push
    # GIT_TERMINAL_PROMPT=0: スケジュールタスク環境で認証プロンプトがハングするのを防ぐ
    _git_env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
    try:
        subprocess.run(['git', 'add', 'docs/index.html'], cwd=str(proj_dir), check=True,
                       env=_git_env)
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        subprocess.run(
            ['git', 'commit', '-m', f'Update prediction {now}'],
            cwd=str(proj_dir), check=True, env=_git_env
        )
        subprocess.run(['git', 'push'], cwd=str(proj_dir), check=True,
                       env=_git_env, timeout=120)
        print(f"  ✅ GitHub Pages デプロイ完了")
        return True
    except subprocess.TimeoutExpired:
        print(f"  ⚠️ git push がタイムアウト(120秒)。認証情報の問題の可能性。")
        print("  手動で git push してください")
        return False
    except subprocess.CalledProcessError as e:
        print(f"  ⚠️ git操作でエラー: {e}")
        print("  手動で git push してください")
        return False


def main():
    parser = argparse.ArgumentParser(description='週末予想パイプライン')
    parser.add_argument('--saturday', action='store_true', help='土曜のみ')
    parser.add_argument('--sunday', action='store_true', help='日曜のみ')
    parser.add_argument('--refresh-odds', action='store_true', help='オッズ再取得+再生成のみ')
    parser.add_argument('--skip-fetch', action='store_true', help='fetch省略')
    parser.add_argument('--no-deploy', action='store_true', help='デプロイ省略')
    parser.add_argument('--date', type=str, help='日付指定（YYYYMMDD）')
    parser.add_argument('--save-results', type=str, metavar='YYYYMMDD',
                        help='指定日の結果を取得して月間JSONに保存')
    args = parser.parse_args()

    proj_dir = PROJ_DIR
    os.chdir(proj_dir)

    sat, sun = get_weekend_dates()

    if args.date:
        dates = [args.date]
    elif args.saturday:
        dates = [sat]
    elif args.sunday:
        dates = [sun]
    else:
        dates = [sat, sun]

    # ── 結果保存モード ──
    if args.save_results:
        date_str = args.save_results
        print(f"🐴 NORISHICO AI 結果保存モード")
        print(f"   対象日: {get_day_label(date_str)}")
        t0 = time.time()

        results_file = step_fetch_results(date_str, proj_dir)

        # 常にweekend_predictions.jsonを使用（HTML生成と同じソース）
        # ※ 以前はsaturday/sunday_predictions.jsonを曜日で分岐していたが、
        #    get_weekend_dates()が実行日基準のため実行タイミングで参照ファイルが変わり、
        #    HTMLとスプシで異なるレースが選ばれるバグがあった
        preds_file = proj_dir / 'weekend_predictions.json'

        if preds_file.exists():
            # レース前の最終判断をスナップショットとして保存
            final_file = preds_file.with_name(preds_file.stem + '_final.json')
            shutil.copy2(preds_file, final_file)
            print(f"  📸 最終予想スナップショット → {final_file.name}")

            step_save_monthly(results_file, final_file, proj_dir)
            step_post_spreadsheet(proj_dir)
        else:
            print(f"  ⚠️ 予想ファイル {preds_file.name} が見つかりません")

        # HTML再生成+デプロイ
        step_html(proj_dir)
        docs = proj_dir / 'docs'
        docs.mkdir(exist_ok=True)
        shutil.copy2(proj_dir / 'this_week_prediction.html', docs / 'index.html')
        if not args.no_deploy:
            step_deploy(proj_dir)

        elapsed = time.time() - t0
        print(f"\n✅ 結果保存完了（{elapsed:.0f}秒）")
        return

    print(f"🐴 ノリシコ競馬AI 週末予想パイプライン")
    print(f"   対象日: {', '.join(get_day_label(d) for d in dates)}")
    print(f"   実行時刻: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    t0 = time.time()

    # Step 1: Fetch
    if not args.skip_fetch:
        step_fetch(dates, proj_dir)
    else:
        print("\n⏭ fetch省略（既存JSONを使用）")

    # Step 2: Predict (prevを保存してから再予測)
    step_save_prev(proj_dir)

    # 初回生成時はアラートログをクリア（--refresh-odds以外）
    if not args.refresh_odds:
        sys.path.insert(0, str(proj_dir))
        from alerts_log import clear_alerts
        clear_alerts()

    if not step_predict(proj_dir):
        print("❌ スコアリングに失敗。中断します。")
        return

    # 差分検出 → アラートログ追記
    new_alerts = step_detect_alerts(proj_dir)
    if new_alerts:
        print(f"  🚨 アラート {len(new_alerts)}件:")
        for a in new_alerts:
            print(f"    {a['text']}")

    # Step 3: HTML
    if not step_html(proj_dir):
        print("❌ HTML生成に失敗。中断します。")
        return

    # Step 4: Note article
    step_note(proj_dir)

    # docs/index.html は常にコピー
    docs = proj_dir / 'docs'
    docs.mkdir(exist_ok=True)
    src = proj_dir / 'this_week_prediction.html'
    if src.exists():
        shutil.copy2(src, docs / 'index.html')
        print(f"\n📄 docs/index.html 更新")

    # Step 5: Deploy
    if not args.no_deploy:
        step_deploy(proj_dir)
    else:
        print("⏭ git push 省略")

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"✅ 全ステップ完了（{elapsed:.0f}秒）")
    print(f"{'='*60}")
    print(f"  📄 this_week_prediction.html  → 予想HTML")
    print(f"  📄 weekend_predictions.json    → 予想データ")
    print(f"  📄 note_article_sat.txt        → Note記事")
    print(f"  📄 docs/index.html             → GitHub Pages用")
    print(f"\n  次のステップ:")
    print(f"    1. note_article_sat.txt の内容を Note.com に投稿")
    print(f"    2. X投稿文を X(Twitter) に投稿")
    if (proj_dir / 'docs' / 'index.html').exists():
        print(f"    3. GitHub PagesのURLでHTML公開済み")


if __name__ == '__main__':
    main()
