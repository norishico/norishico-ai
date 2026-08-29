"""
generate_mc_record.py — ウィジェット用データ(widget_data.json)生成
python generate_mc_record.py [YYYY-MM-DD]

2026-08-10: AYOkeibaサイトの「記録履歴」タブ(旧v6.6軸馬/相手馬買い目表示)を撤去したのに伴い、
本スクリプトの役割もiPhoneウィジェット用データ生成のみに縮小。旧役割だったmc_record.html/
index.htmlへのconst RACES埋め込みと、そのためのMCシミュレーション計算(run_mc_3pat等)は
削除した(Scriptableウィジェット側がjiku_name/aiteを一切描画していないことを確認済みのため、
DB接続・モンテカルロ計算・HTML書き換えのすべてが不要だった)。
"""
import sys
import json
from pathlib import Path
from datetime import date as _date

sys.stdout.reconfigure(encoding='utf-8')

TARGET_DATE = sys.argv[1] if len(sys.argv) > 1 else _date.today().isoformat()

# 2026-08-29障害対応: 前夜のSaturdayPreview(publish_weekend.py --saturday)がnetkeiba取得
# エラーでfail-loud終了し、weekend_predictions.json/this_week_races.jsonが5日前のまま
# 残留した状態で本スクリプト(パイプライン最上流)が翌朝走り、「本日該当レース0件」を
# 正常終了として出力→後続のpace/mc123/win5生成とvercel deployが空データのまま実行され、
# AYOkeibaサイトが空表示になった。この再発防止として、後続処理に進む前にweekend_predictions.
# json(pace/mc123タブ側でも「検証済みの優先ソース」として扱っている、predict_weekend.pyが
# 本体予想生成の最終工程で書き出すファイル)の鮮度を検証する。
STALE_GAP_DAYS = 3  # weekend_predictions.jsonの最新日付が対象日よりこれ以上前なら「データ古着」とみなす


def check_weekend_predictions_freshness():
    """weekend_predictions.jsonに対象日(TARGET_DATE)のレースが実際に含まれているかを検証する。

    - 対象日のレースが1件でもあれば正常(そのまま続行)。
    - 対象日のレースが0件で、かつファイル内の最新日付が対象日から3日以上前の場合は、
      本体予想生成(SaturdayPreview/SundayPreview/MondayPreview等のpublish_weekend.py)が
      失敗して更新されていない可能性が高いためエラー終了する(exit code非0)。
      mc_keiba_generate.bat側の `if %RC% NEQ 0 goto :end` により、後続のpace/mc123/win5
      生成とvercel deployも連鎖的にスキップされる。
    - 対象日のレースが0件でも、最新日付が対象日の近く(3日未満)の場合は「今回は本当に
      開催が無い日」とみなして続行する(平日等での誤検知防止)。
    - ファイルが存在しない/空の場合は判定不能として続行する(初回セットアップ等を誤検知しない)。
    """
    p = Path('weekend_predictions.json')
    if not p.exists():
        print('weekend_predictions.json: ファイルが存在しないため鮮度チェックをスキップします')
        return
    try:
        wp_all = json.load(open(p, encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as e:
        print(f'エラー: weekend_predictions.jsonの読み込みに失敗しました: {e}', file=sys.stderr)
        sys.exit(1)

    dates = sorted({(entry.get('race') or {}).get('date') for entry in wp_all
                     if (entry.get('race') or {}).get('date')})
    target_hit = sum(1 for entry in wp_all if (entry.get('race') or {}).get('date') == TARGET_DATE)

    if target_hit > 0:
        print(f'weekend_predictions.json: 対象日({TARGET_DATE})のレース{target_hit}件を確認')
        return
    if not dates:
        print('weekend_predictions.json: レースデータが空のため鮮度チェックをスキップします')
        return

    latest_date = dates[-1]
    gap_days = abs((_date.fromisoformat(TARGET_DATE) - _date.fromisoformat(latest_date)).days)
    if gap_days >= STALE_GAP_DAYS:
        print(f'エラー: weekend_predictions.jsonに対象日({TARGET_DATE})のデータがありません。'
              f'ファイル内の最新日付は{latest_date}で、{gap_days}日前のまま更新されていません。',
              file=sys.stderr)
        print('weekend_predictions.jsonに対象日のデータが無い。SaturdayPreview/SundayPreview/'
              'MondayPreview等の本体予想生成(publish_weekend.py)が失敗している可能性がある。'
              'logs/配下の該当タスクログを確認すること。', file=sys.stderr)
        sys.exit(1)

    print(f'weekend_predictions.json: 対象日({TARGET_DATE})のレースなし '
          f'(最新日付{latest_date}, {gap_days}日差) → 開催日でないと判断し続行します')


def load_start_time_map():
    """this_week_races.jsonからvenue×race_num→{start_time, nk_id}のマップを返す"""
    p = Path('this_week_races.json')
    if not p.exists():
        return {}
    races = json.load(open(p, encoding='utf-8'))
    return {(r['venue'], r['race_num']): {
        'start_time': r.get('start_time', ''),
        'nk_id': r.get('race_id', ''),
    } for r in races if r.get('date') == TARGET_DATE}


def fetch_target_races():
    """this_week_races.jsonから当日の対象レースを取得。
    AYOkeiba(展開予想/MC123タブ)と同一基準: 芝・ダート全レース、新馬・障害のみ除外。"""
    st_map = load_start_time_map()
    print(f'発走時刻マップ: {len(st_map)}件 (this_week_races.json)')

    p = Path('this_week_races.json')
    all_json = json.load(open(p, encoding='utf-8')) if p.exists() else []
    json_races = [r for r in all_json
                  if r.get('date') == TARGET_DATE and r.get('surface') in ('芝', 'ダ')]

    def is_valid_race(r):
        rname = r.get('race_name') or ''
        return '新馬' not in rname and '障害' not in rname

    json_races = [r for r in json_races if is_valid_race(r)]
    print(f'新馬・障害除外後: {len(json_races)}件')

    out = []
    for race in sorted(json_races, key=lambda r: (r.get('venue', ''), r.get('race_num', 0))):
        venue = race.get('venue', '')
        rno = race.get('race_num', 0)
        dst = race.get('distance') or 1600
        rname = race.get('race_name') or f'{dst}mダート'
        n_horses = len(race.get('horses', []))
        if n_horses < 4:
            continue
        info = st_map.get((venue, rno), {})
        st = info.get('start_time') or race.get('start_time') or f'{10 + rno // 2}:00'
        nk_id = info.get('nk_id', '') or race.get('race_id', '')
        out.append({
            'venue': venue, 'rno': rno, 'rname': rname,
            'start_time': st, 'nk_id': nk_id,
        })
    return out


def write_widget_json(races):
    """docs/widget_data.json を書き出す (GitHub Pages経由でScriptableウィジェットが取得)"""
    data = {'date': TARGET_DATE, 'races': races}
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    docs = Path('docs')
    docs.mkdir(exist_ok=True)
    (docs / 'widget_data.json').write_text(payload, encoding='utf-8')
    pub = Path('mc_keiba_public')
    if pub.exists():
        (pub / 'widget_data.json').write_text(payload, encoding='utf-8')
    print(f'widget_data.json書き出し完了: {len(races)}件')


def main():
    check_weekend_predictions_freshness()
    races = fetch_target_races()
    write_widget_json(races)
    nk_empty = [f"{r['venue']}{r['rno']}R" for r in races if not r.get('nk_id')]
    if nk_empty:
        print(f'nk_id未設定: {nk_empty}')
    else:
        print('nk_id全件設定済み')


if __name__ == '__main__':
    main()
