# -*- coding: utf-8 -*-
"""
generate_win5_data.py — AYOkeibaサイト用のWIN5対象レース情報データ生成(2026-08-22新設)

指定日のWIN5対象レース(通常5レース、複数会場にまたがることがある)を
https://race.netkeiba.com/top/win5.html?date=YYYYMMDD から取得し、
weekend_predictions.json(v6.6本命)・mc_keiba_public/mc123_data.json(MC123複勝内候補)
と突合して mc_keiba_public/win5_data.json に書き出す。

設計方針(のりお承認 2026-08-22):
- 買い目としては提示しない、情報提供専用。「本命(v6.6予想)」と「AI3着以内候補(MC123)」を
  別々に表示し、それぞれ何を根拠にしているかフロント側で区別できるようにする
- weekend_predictions.jsonとのつき合わせはrace_id(JRA 12桁コード)の完全一致で行う。
  WIN5対象レースのrace_idはJRA公式コードそのもの(venue/kaisai/day/raceの4パーツ)で
  weekend_predictions.json側の生成元(fetch_and_build.py系)と同一体系のため、
  文字列比較で問題ない(注目レースタブがmc123_data.json側で使ったvenue+rno突合とは
  事情が異なる。mc123_data.json自体はrace_id形式が違う `YYYY-MM-DD_venue_rno` なので
  そちらとはvenue+race_num(rno)で突合する)
- 突合に失敗した場合は捏造せず、matched_prediction/matched_mc123をfalseにしてフロント側で
  「予想データなし」を出させる
- netkeiba.comへのアクセスは1日1回程度の手動/バッチ実行を想定(WIN5対象レースは開催日に
  1回確定すれば変わらないため、pace_data.json等のように頻繁な再生成は不要)

使い方: py -3 generate_win5_data.py [YYYYMMDD]  (省略時は当日)
"""
import sys
import json
import re
import requests
from datetime import date as _date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WEEKEND_PRED_PATH = BASE_DIR / "weekend_predictions.json"
MC123_PATH = BASE_DIR / "mc_keiba_public" / "mc123_data.json"
OUT_PATHS = [BASE_DIR / "mc_keiba_public" / "win5_data.json"]

TARGET_DATE_ARG = sys.argv[1] if len(sys.argv) > 1 else _date.today().strftime("%Y%m%d")
TARGET_DATE_ISO = f"{TARGET_DATE_ARG[0:4]}-{TARGET_DATE_ARG[4:6]}-{TARGET_DATE_ARG[6:8]}"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
WIN5_URL = f"https://race.netkeiba.com/top/win5.html?date={TARGET_DATE_ARG}"

# 対象レースの1マス分: <a href="../race/shutuba.html?race_id=202607030106">中京6R<br>大府特別</a>
RACE_CELL_RE = re.compile(
    r'race_id=(\d{12})">([^\d<]+?)(\d+)R<br>([^<]*)</a>'
)


def fetch_win5_target_races(date_str):
    """WIN5対象レース(通常5レース)をnetkeibaから取得する。

    2026-08-22確認: このページはUTF-8で配信される(過去のfetch_win5_history.pyが結果
    取得に使っていたeuc-jpとは異なる。実際にeuc-jpでデコードすると文字化けすることを
    実機取得で確認済み)。取得できない/対象レースが無い日はNoneを返す(ex: 平日開催なし)。
    """
    r = requests.get(WIN5_URL, headers=HEADERS, timeout=15)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    html = r.content.decode("utf-8", errors="replace")

    # 「対象レース」見出しの直後のwin5raceresult2テーブルの「レース」行だけを見る
    # (勝ち馬・単勝人気・残り票数の行にはrace_idが含まれないため、テーブル全体を
    # 対象にしても実害はないが、意図を明確にするため先頭のテーブルのみを切り出す)
    m = re.search(r'win5raceresult2.*?</table>', html, re.S)
    if not m:
        return None, "対象レーステーブルが見つからない(WIN5非開催日の可能性)"
    table_html = m.group(0)

    cells = RACE_CELL_RE.findall(table_html)
    if not cells:
        return None, "対象レースのセルを抽出できなかった"

    races = []
    for i, (race_id, venue, rno, rname) in enumerate(cells):
        races.append({
            "leg": i + 1,
            "race_id": race_id,
            "venue": venue.strip(),
            "race_num": int(rno),
            "race_name": rname.strip(),
        })
    return races, None


def load_weekend_predictions():
    if not WEEKEND_PRED_PATH.exists():
        return {}
    data = json.loads(WEEKEND_PRED_PATH.read_text(encoding="utf-8"))
    lut = {}
    for item in data:
        race = item.get("race") or {}
        rid = race.get("race_id")
        if rid:
            lut[rid] = item
    return lut


def load_mc123_data():
    if not MC123_PATH.exists():
        return {}
    data = json.loads(MC123_PATH.read_text(encoding="utf-8"))
    lut = {}
    for race in data.get("races", []):
        key = (race.get("venue"), race.get("rno"))
        lut[key] = race
    return lut


def build_honmei_entry(item):
    h = item.get("honmei") or {}
    if not h:
        return None
    return {
        "horse_name": h.get("horse_name"),
        "umaban": h.get("horse_num"),
        "waku": h.get("waku"),
        "odds": h.get("odds"),
        "total_score": h.get("total_score"),
        "popularity": h.get("popularity"),
    }


def build_mc123_top3(race, track_label="良・稍重", top_n=3):
    horses = race.get("horses") or []
    ranked = sorted(
        horses,
        key=lambda h: -((h.get("patterns") or {}).get(track_label, {}).get("ptop3") or 0),
    )
    out = []
    for h in ranked[:top_n]:
        pat = (h.get("patterns") or {}).get(track_label, {})
        out.append({
            "num": h.get("num"),
            "waku": h.get("waku"),
            "name": h.get("name"),
            "style": h.get("style"),
            "ptop3": pat.get("ptop3"),
        })
    return out


def main():
    print(f"WIN5対象レース取得中: {WIN5_URL}")
    target_races, err = fetch_win5_target_races(TARGET_DATE_ARG)

    if target_races is None:
        print(f"取得失敗/対象なし: {err}")
        payload = {
            "date": TARGET_DATE_ISO,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source_url": WIN5_URL,
            "note": "WIN5対象レースの情報です。買い目(推奨馬券)ではなく参考情報です。",
            "n_races_target": 0,
            "fetch_error": err,
            "races": [],
        }
        for p in OUT_PATHS:
            if p.parent.exists():
                p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
                print(f"書き出し(対象なし): {p}")
        return

    print(f"WIN5対象レース {len(target_races)}件取得:")
    for r in target_races:
        print(f"  {r['leg']}レース目: {r['venue']}{r['race_num']}R {r['race_name']} (race_id={r['race_id']})")

    pred_lut = load_weekend_predictions()
    mc123_lut = load_mc123_data()
    print(f"weekend_predictions.json: {len(pred_lut)}レース読込")
    print(f"mc123_data.json: {len(mc123_lut)}レース読込")

    races_out = []
    n_matched_pred, n_matched_mc123 = 0, 0
    for r in target_races:
        entry = {
            "leg": r["leg"],
            "race_id": r["race_id"],
            "venue": r["venue"],
            "race_num": r["race_num"],
            "race_name": r["race_name"],
            "matched_prediction": False,
            "matched_mc123": False,
        }

        item = pred_lut.get(r["race_id"])
        if item is not None:
            race_info = item.get("race") or {}
            entry["matched_prediction"] = True
            entry["start_time"] = race_info.get("start_time")
            entry["surface"] = race_info.get("surface")
            entry["distance"] = race_info.get("distance")
            entry["track_cond"] = race_info.get("track_cond")
            entry["grade"] = item.get("grade")
            entry["buy_type"] = item.get("buy_type")
            entry["honmei"] = build_honmei_entry(item)
            n_matched_pred += 1
        else:
            print(f"  WARN: weekend_predictions.jsonに{r['venue']}{r['race_num']}R"
                  f"(race_id={r['race_id']})が見つからない")

        mc123_race = mc123_lut.get((r["venue"], r["race_num"]))
        if mc123_race is not None:
            entry["matched_mc123"] = True
            if not entry.get("race_name"):
                entry["race_name"] = mc123_race.get("rname")
            entry.setdefault("surface", mc123_race.get("surface"))
            entry.setdefault("distance", mc123_race.get("distance"))
            entry["mc123_top3"] = build_mc123_top3(mc123_race)
            n_matched_mc123 += 1
        else:
            print(f"  WARN: mc123_data.jsonに{r['venue']}{r['race_num']}Rが見つからない")

        races_out.append(entry)

    payload = {
        "date": TARGET_DATE_ISO,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_url": WIN5_URL,
        "note": "WIN5対象レースの情報です。買い目(推奨馬券)ではなく参考情報です。"
                "「本命」はv6.6予想ロジックの本命馬、「AI3着以内候補」はMC123シミュレーション"
                "(オッズ非考慮・情報提供専用)による複勝圏内率上位馬です。",
        "n_races_target": len(target_races),
        "races": races_out,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    for p in OUT_PATHS:
        if p.parent.exists():
            p.write_text(text, encoding="utf-8")
            print(f"書き出し: {p}")
    print(f"完了: 対象{len(target_races)}レース中、v6.6予想突合{n_matched_pred}件・MC123突合{n_matched_mc123}件")


if __name__ == "__main__":
    main()
