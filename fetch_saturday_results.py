"""fetch_saturday_results.py
土曜(または指定日)のレース結果を sp.netkeiba.com からスクレイプしてkeiba.dbを更新。
- requests + BeautifulSoup (EUC-JP) で完結。Selenium不要
- race.netkeiba.com がIPブロックされていても動作可能
- results.finish / umaban / track_cond を horse_name照合で更新
- JVLink正式データ(23:30 fetch_and_build)が来るまでの暫定補完

Usage:
  python fetch_saturday_results.py               # 昨日
  python fetch_saturday_results.py --date 20260425
  python fetch_saturday_results.py --dry-run
"""
import argparse
import datetime as dt
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
DB_PATH = ROOT / "keiba.db"
THIS_WEEK_JSON = ROOT / "this_week_races.json"

SP_BASE = "https://race.sp.netkeiba.com/"
MOBILE_UA = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
    'Accept-Language': 'ja,en',
    'Accept': 'text/html,application/xhtml+xml',
}

# 性別コード (馬名末尾から除去)
SEX_CHARS = '牡牝セ'


# JRA公式race_id(12桁: YYYY+venue_cd2桁+kai2桁+nichime2桁+race_num2桁)の
# venue_cdから会場名を導出するマッピング(fetch_shutsuba_sp.pyと同一定義)
_VENUE_MAP = {'01': '札幌', '02': '函館', '03': '福島', '04': '新潟', '05': '東京',
              '06': '中山', '07': '中京', '08': '京都', '09': '阪神', '10': '小倉'}


def get_race_ids_for_date(date_str):
    """指定日のrace情報リストを返す(this_week_races.json優先、無ければSeleniumで
    netkeibaから遡及取得)。
    date_str: 'YYYY-MM-DD'
    Returns: [{'race_id': str, 'venue': str, 'race_num': int}, ...]

    【2026-09-01追加】this_week_races.jsonは直近週のみ保持しており、過去分の
    umaban遡及復旧(build_db.pyのINSERT OR REPLACEバグにより長期間umaban未更新
    だった問題への対応)にはthis_week_races.json経由では対応できない。
    Seleniumフォールバックを追加し、race_id(JRA公式12桁)から機械的にvenue/race_num
    を導出することで、this_week_races.jsonに依存せず任意の過去日を処理可能にする。
    """
    if THIS_WEEK_JSON.exists():
        try:
            with open(THIS_WEEK_JSON, encoding='utf-8') as f:
                races = json.load(f)
            day_races = [r for r in races if r.get('date', '') == date_str]
            if day_races:
                print(f"this_week_races.json から {date_str} の {len(day_races)}R を取得")
                return [{'race_id': r['race_id'], 'venue': r.get('venue', ''), 'race_num': r.get('race_num', 0)} for r in day_races]
        except Exception as e:
            print(f"this_week_races.json 読み込みエラー: {e}")

    print(f"this_week_races.jsonに{date_str}のデータなし。Seleniumでnetkeibaから遡及取得を試みます")
    try:
        from fetch_shutsuba import create_driver, fetch_race_list
        yyyymmdd = date_str.replace('-', '')
        driver = create_driver()
        try:
            raw_races = fetch_race_list(driver, yyyymmdd)
        finally:
            driver.quit()
        out = []
        for r in raw_races:
            rid = r.get('race_id', '')
            if len(rid) != 12 or not rid.isdigit():
                continue
            venue_cd = rid[4:6]
            venue = _VENUE_MAP.get(venue_cd)
            if not venue:
                continue
            race_num = int(rid[10:12])
            out.append({'race_id': rid, 'venue': venue, 'race_num': race_num})
        if out:
            print(f"Seleniumで {date_str} の {len(out)}R を取得")
            return out
    except Exception as e:
        print(f"Selenium遡及取得エラー: {e}")
    return None


def strip_sex(name_text):
    """馬名末尾の性別+年齢+馬体重を除去して純粋な馬名を返す
    例: 'コーカサスゴールド牡5576k' → 'コーカサスゴールド'
    性別文字が馬名中に含まれる場合の誤切断を防ぐため、
    性別文字の直後が数字の場合のみ除去する。
    """
    return re.sub(r'[' + SEX_CHARS + r']\d.*$', '', name_text).strip()


def _parse_time(time_str):
    """'1:32.1' → 92.1秒 に変換。失敗時はNone"""
    if not time_str:
        return None
    try:
        m = re.match(r'^(\d+):(\d+\.\d+)$', time_str.strip())
        if m:
            return round(int(m.group(1)) * 60 + float(m.group(2)), 1)
        f = float(time_str.strip())
        return round(f, 1) if f > 0 else None
    except Exception:
        return None


def fetch_result_sp(race_id):
    """netkeiba PC版結果ページをスクレイプ（枠番・タイム・着差・上がり3Fも取得）
    Returns: {
        'track_cond': str,
        'horses': [{'finish': int, 'waku': int, 'umaban': int, 'name': str,
                    'time_sec': float, 'margin': str, 'last3f': float}]
    } or None
    セル配置: [着順, 枠, 馬番, 馬名, 性齢, 斤量, 騎手, タイム, 着差, 人気, 単勝, 上がり3F, ...]
    """
    # まず PC版 race.netkeiba.com を試す（SP版より安定・詳細データあり）
    PC_URL = f"https://race.netkeiba.com/race/result.html?race_id={race_id}"
    SP_URL = f"{SP_BASE}?pid=race_result&race_id={race_id}"

    PC_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    soup = None
    for url, ua, enc in [(PC_URL, PC_UA, 'euc-jp'), (SP_URL, MOBILE_UA, 'euc-jp')]:
        try:
            r = requests.get(url, headers=ua, timeout=15)
            if r.status_code == 200 and len(r.content) > 1000:
                soup = BeautifulSoup(r.content, 'html.parser', from_encoding=enc)
                break
        except Exception:
            continue

    if soup is None:
        print(f"    取得失敗: {race_id}")
        return None

    info = {'track_cond': '', 'horses': []}

    # 馬場状態
    try:
        for cls in ['Item04', 'RaceData02', 'RaceData01']:
            el = soup.find(class_=cls)
            if el:
                text = el.get_text()
                for tc in ['稍重', '重', '不良', '良']:
                    if tc in text:
                        info['track_cond'] = tc
                        break
                if info['track_cond']:
                    break
    except Exception:
        pass

    # 着順テーブルを探す（PC版・SP版両対応）
    try:
        result_table = None
        for cls_pat in [r'RaceCommon', r'race_table', r'ResultTable']:
            result_table = soup.find('table', class_=re.compile(cls_pat))
            if result_table:
                break
        if not result_table:
            # すべてのtableから着順行を持つものを探す
            for t in soup.find_all('table'):
                rows = t.find_all('tr')
                for row in rows:
                    cells = row.find_all('td')
                    if cells and re.match(r'^\d+着?$', cells[0].get_text(strip=True)):
                        result_table = t
                        break
                if result_table:
                    break

        if not result_table:
            return None

        for row in result_table.find_all('tr'):
            cells = row.find_all('td')
            if len(cells) < 3:
                continue
            finish_text = cells[0].get_text(strip=True)
            # '1' または '1着' 両方対応
            m = re.match(r'^(\d+)着?$', finish_text)
            if not m:
                continue
            finish = int(m.group(1))

            # 枠番 cells[1]
            waku = None
            if len(cells) > 1:
                wt = cells[1].get_text(strip=True)
                waku = int(wt) if wt.isdigit() else None

            # 馬番 cells[2]
            umaban = None
            if len(cells) > 2:
                ut = cells[2].get_text(strip=True)
                umaban = int(ut) if ut.isdigit() else None

            # 馬名 cells[3]
            raw_name = cells[3].get_text(strip=True) if len(cells) > 3 else ''
            horse_name = strip_sex(raw_name)
            if not horse_name:
                continue

            # タイム cells[7]
            time_sec = None
            if len(cells) > 7:
                time_sec = _parse_time(cells[7].get_text(strip=True))

            # 着差 cells[8]
            margin = None
            if len(cells) > 8:
                mg = cells[8].get_text(strip=True)
                margin = mg if mg else None

            # 上がり3F cells[11]
            last3f = None
            if len(cells) > 11:
                lt = cells[11].get_text(strip=True)
                try:
                    last3f = float(lt) if lt else None
                except Exception:
                    pass

            # 通過順位 cells[12]: "3-3-2-2" or "2-2" 形式
            pos1 = pos2 = pos3 = pos4 = None
            if len(cells) > 12:
                passage_raw = cells[12].get_text(strip=True)
                parts = passage_raw.split('-')
                try:
                    if len(parts) == 4:
                        pos1, pos2, pos3, pos4 = [int(p) for p in parts]
                    elif len(parts) == 2:
                        pos3, pos4 = int(parts[0]), int(parts[1])
                    elif len(parts) == 1 and parts[0].isdigit():
                        pos4 = int(parts[0])
                except Exception:
                    pass

            info['horses'].append({
                'finish': finish, 'waku': waku, 'umaban': umaban, 'name': horse_name,
                'time_sec': time_sec, 'margin': margin, 'last3f': last3f,
                'pos1': pos1, 'pos2': pos2, 'pos3': pos3, 'pos4': pos4,
            })

    except Exception as e:
        print(f"    Parse error {race_id}: {e}")
        return None

    if not info['horses']:
        return None
    winner = info['horses'][0]
    print(f"    track={info['track_cond']} 1着:{winner['name']} ({len(info['horses'])}頭) "
          f"time={winner.get('time_sec')} L3={winner.get('last3f')}")
    return info


def update_db(conn, date_str, venue, race_num, race_info, dry_run=False):
    """results の各フィールドを horse_name照合で更新
    更新対象: finish, umaban, pos_col(枠番), time_sec(全馬), margin(着差), last3f(上がり3F), track_cond
    JVLinkデータが後から来た場合は上書きしない（JVLink後勝ちルール: 各カラムがNULLの間のみ更新）。

    【2026-08-31修正】以前は「finish IS NULLの間だけ」を更新条件・対象行の絞り込み両方に
    使っていたが、これは「finishが確定していればumabanも確定している」という誤った前提に
    基づいていた。実際にはumaban(実馬番)はJV-Link側のresultsカラムマッピングに存在せず、
    このスクリプトの更新でしか埋まらない仕様(build_db.py参照)。そのためJV-Link経由で
    finishだけ先に確定すると、本関数の対象行から漏れてumabanが永久に埋まらなくなっていた
    (2026-07頃〜2026-08-30、8/1を除きほぼ全期間でumaban未更新という実害が発生)。
    finish/umabanそれぞれ個別にNULLの間だけ更新し、行の絞り込みも「いずれかがNULL」に
    緩和することで、finish確定後でもumabanだけを後から埋められるようにする。
    """
    db_race_id = f"{date_str}_{venue}_{race_num}"
    track_cond = race_info['track_cond']

    if track_cond and not dry_run:
        conn.execute(
            "UPDATE results SET track_cond=? WHERE race_id=? AND track_cond IS NULL",
            (track_cond, db_race_id)
        )

    finish_updated = 0
    for h in race_info['horses']:
        if not h['name']:
            continue
        if not dry_run:
            # 各カラムがNULLの間だけ個別に暫定データを書き込む（JVLink後勝ちルール）
            cur = conn.execute(
                """UPDATE results
                   SET finish=CASE WHEN finish IS NULL THEN ? ELSE finish END,
                       umaban=CASE WHEN umaban IS NULL THEN ? ELSE umaban END,
                       pos_col=CASE WHEN pos_col IS NULL THEN ? ELSE pos_col END,
                       time_sec=CASE WHEN time_sec IS NULL THEN ? ELSE time_sec END,
                       margin=CASE WHEN margin IS NULL THEN ? ELSE margin END,
                       last3f=CASE WHEN last3f IS NULL THEN ? ELSE last3f END,
                       pos1=CASE WHEN pos1 IS NULL THEN ? ELSE pos1 END,
                       pos2=CASE WHEN pos2 IS NULL THEN ? ELSE pos2 END,
                       pos3=CASE WHEN pos3 IS NULL THEN ? ELSE pos3 END,
                       pos4=CASE WHEN pos4 IS NULL THEN ? ELSE pos4 END
                   WHERE race_id=?
                     AND TRIM(horse_name)=TRIM(?)
                     AND (finish IS NULL OR umaban IS NULL)""",
                (h['finish'], h.get('umaban'), h.get('waku'),
                 h.get('time_sec'), h.get('margin'), h.get('last3f'),
                 h.get('pos1'), h.get('pos2'), h.get('pos3'), h.get('pos4'),
                 db_race_id, h['name'])
            )
            if cur.rowcount > 0:
                finish_updated += 1
        else:
            finish_updated += 1

    if not dry_run:
        conn.commit()
    return finish_updated, finish_updated  # 後方互換のため2値返却


def backup_db():
    bak = DB_PATH.with_suffix(f".db.bak_sat_results_{dt.date.today().strftime('%Y%m%d')}")
    if not bak.exists():
        shutil.copy2(str(DB_PATH), str(bak))
        print(f"Backup: {bak.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='YYYYMMDD (default: yesterday)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if args.date:
        target_date = dt.datetime.strptime(args.date, '%Y%m%d').date()
    else:
        target_date = dt.date.today() - dt.timedelta(days=1)

    date_str = target_date.strftime('%Y-%m-%d')

    print(f"=== fetch_saturday_results.py: {date_str} {'[DRY-RUN]' if args.dry_run else ''} ===")

    conn = sqlite3.connect(str(DB_PATH))
    # 【2026-08-31修正】finish IS NULLのみで判定すると、finishはJV-Link経由で確定済み
    # だがumabanが未確定、というケースを「処理不要」で見逃してしまう(update_db()参照)。
    null_count = conn.execute(
        "SELECT COUNT(*) FROM results WHERE date=? AND (finish IS NULL OR umaban IS NULL)",
        (date_str,)
    ).fetchone()[0]
    total_count = conn.execute(
        "SELECT COUNT(*) FROM results WHERE date=?", (date_str,)
    ).fetchone()[0]
    print(f"DB: {date_str} results={total_count}件 / finish or umaban 未確定={null_count}件")

    if null_count == 0:
        print("全レースfinish・umaban済み。処理不要。")
        conn.close()
        return 0

    race_list = get_race_ids_for_date(date_str)
    if not race_list:
        print(f"this_week_races.jsonに{date_str}のデータなし")
        conn.close()
        return 1

    if not args.dry_run:
        backup_db()

    total_finish = 0
    total_umaban = 0
    total_races = 0

    for race in race_list:
        rid = race['race_id']
        venue = race['venue']
        race_num = race['race_num']
        print(f"  {venue}{race_num}R ({rid})")

        result = fetch_result_sp(rid)
        time.sleep(1)  # サーバーに優しく

        if not result or not result['horses']:
            print(f"    skip: no data")
            continue

        n_finish, n_umaban = update_db(conn, date_str, venue, race_num, result, dry_run=args.dry_run)
        total_finish += n_finish
        total_umaban += n_umaban
        total_races += 1
        print(f"    → finish更新 {n_finish}件 / umaban更新 {n_umaban}件")

    conn.close()

    print(f"\n=== 完了 ===")
    print(f"処理レース: {total_races}R / finish更新: {total_finish}件 / umaban更新: {total_umaban}件")

    if not args.dry_run:
        conn2 = sqlite3.connect(str(DB_PATH))
        still_null = conn2.execute(
            "SELECT COUNT(*) FROM results WHERE date=? AND finish IS NULL", (date_str,)
        ).fetchone()[0]
        conn2.close()
        print(f"残りNULL: {still_null}件")

    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
