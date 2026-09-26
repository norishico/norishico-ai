# -*- coding: utf-8 -*-
"""
build_course_tiers_json.py — mc123_top1_reliability.json と formation_accuracy.json を
(venue, surface, distance) で外部結合し、AYOkeibaサイトの「コース一覧」タブ用に
mc_keiba_public/course_tiers.json を出力する(2026-09-23新規、S6)。

週次(水曜6:00 NorishikoAI_MC123TierRefresh)のtier再計算直後、scripts/mc123_tier_refresh.bat
末尾から呼ばれる。両ファイルは compute_mc123_top1_reliability.py / compute_formation_accuracy.py
が生成する日付非依存の静的統計テーブル(generate_mc_record.pyのload_tier_lookup()と同じ読み方)。

both_high判定(1位予想馬の複勝的中率tier・隊列予測精度tierの両方が「高」)のロジックは
generate_mc_record.py の load_tier_lookup()/fetch_target_races()(L94-103・L145、
「注目レース」タブの選出基準そのもの)を踏襲する。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).resolve().parent
RELIABILITY_PATH = ROOT / 'mc123_top1_reliability.json'
ACCURACY_PATH = ROOT / 'formation_accuracy.json'
OUT_PATH = ROOT / 'mc_keiba_public' / 'course_tiers.json'


def load_reliability():
    """mc123_top1_reliability.json を読み込み、(venue, surface, distance) -> cell の辞書と
    メタ情報(生成時刻・使用レース数)を返す。ファイルが無ければ空辞書+Noneメタで返す
    (generate_mc_record.load_tier_lookup()と同様、存在しない場合でも落ちない)。"""
    if not RELIABILITY_PATH.exists():
        return {}, {'generated_at': None, 'n_races': 0, 'scope': None}
    data = json.loads(RELIABILITY_PATH.read_text(encoding='utf-8'))
    lut = {(c['venue'], c['surface'], c['distance']): c for c in data.get('cells', [])}
    meta = {
        'generated_at': data.get('source_generated_at'),
        'n_races': data.get('n_races_used', 0),
        'scope': data.get('scope'),
    }
    return lut, meta


def load_accuracy():
    """formation_accuracy.json を読み込み、(venue, surface, distance) -> cell の辞書と
    メタ情報(使用レース数・ファイルmtime)を返す。この形式のファイルには生成時刻フィールドが
    無いため、ファイルのmtimeをformation_generated_atとして代わりに使う(S6仕様)。"""
    if not ACCURACY_PATH.exists():
        return {}, {'generated_at': None, 'n_races': 0}
    data = json.loads(ACCURACY_PATH.read_text(encoding='utf-8'))
    lut = {(c['venue'], c['surface'], c['distance']): c for c in data.get('cells', [])}
    mtime = datetime.fromtimestamp(ACCURACY_PATH.stat().st_mtime).isoformat(timespec='seconds')
    meta = {
        'generated_at': mtime,
        'n_races': data.get('n_races_used', 0),
    }
    return lut, meta


def build_cells(rel_lut, acc_lut):
    """(venue, surface, distance)の和集合で外部結合する。片方にしか無いセルはrel/accの
    無い側をNoneにする(both_highは必ずFalseになる、generate_mc_record.fetch_target_races()の
    「reliability_lut.get(key) != '高' or accuracy_lut.get(key) != '高'なら除外」と同じ挙動)。
    並び順は会場→芝/ダ→距離(S7の既定表示順と一致させ、フロント側の追加ソートを不要にする)。"""
    keys = set(rel_lut.keys()) | set(acc_lut.keys())

    def rel_entry(c):
        if not c:
            return None
        return {
            'tier': c.get('tier'), 'n': c.get('n'),
            'place_rate': c.get('place_rate_shrunk'),
            'data_limited': bool(c.get('data_limited')),
        }

    def acc_entry(c):
        if not c:
            return None
        return {
            'tier': c.get('tier'), 'n': c.get('n'),
            'rho': c.get('rho_pos4_shrunk'),
            'data_limited': bool(c.get('data_limited')),
        }

    cells = []
    for key in keys:
        venue, surface, distance = key
        rc = rel_lut.get(key)
        ac = acc_lut.get(key)
        rel = rel_entry(rc)
        acc = acc_entry(ac)
        both_high = bool(rel and acc and rel['tier'] == '高' and acc['tier'] == '高')
        cells.append({
            'venue': venue, 'surface': surface, 'distance': distance,
            'rel': rel, 'acc': acc, 'both_high': both_high,
        })

    # 会場名はJIS/五十音順の厳密なソートテーブルが無いため、文字列の自然順(localeCompare相当)で
    # 十分。distanceは数値昇順。venue名の並びはフロント側(S7)で見慣れた表示になれば良く、
    # 買い条件・スコアリングに一切影響しない表示専用データのため、シンプルな安定ソートで良い。
    cells.sort(key=lambda c: (c['venue'], c['surface'], c['distance']))
    return cells


def main():
    rel_lut, rel_meta = load_reliability()
    acc_lut, acc_meta = load_accuracy()
    cells = build_cells(rel_lut, acc_lut)
    both_high_count = sum(1 for c in cells if c['both_high'])

    payload = {
        'generated_at': rel_meta['generated_at'],
        'formation_generated_at': acc_meta['generated_at'],
        'reliability_n_races': rel_meta['n_races'],
        'formation_n_races': acc_meta['n_races'],
        'scope': rel_meta.get('scope'),
        'cells': cells,
    }

    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'course_tiers.json書き出し完了: {len(cells)}セル(both_high={both_high_count}件) -> {OUT_PATH}')
    print(f'  reliability: generated_at={rel_meta["generated_at"]} n_races={rel_meta["n_races"]} cells={len(rel_lut)}')
    print(f'  accuracy(mtime): generated_at={acc_meta["generated_at"]} n_races={acc_meta["n_races"]} cells={len(acc_lut)}')


if __name__ == '__main__':
    main()
