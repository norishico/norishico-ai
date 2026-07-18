"""枠順・脚質バイアステーブル(gate_style_bias.json)の構築

scoring.py の score_gate_style() が読み込む venue×surface×distance 別の
枠順グループ／脚質の勝率差分テーブルを実データから構築する。
gate_style_bias.json は過去(2026-07-11監査時点)一度も生成されたことが無く、
score_gate_style() は常に固定値50.0を返し続けていた(死んでいた要素)。

他の build_*_bonus 系(build_trackcond_damsire_bonus.py 等)と同型:
cutoff_date で未来データを遮断し、年度別リビルドでリークを防止。

枠グループ: _horse_num_to_gate_label と同一 (内(1-2)/中内(3-4)/中(5-7)/中外(8-11)/外(12-14)/大外(15+))
脚質: そのレース自身の pos4/num_horses 比から算出 (_infer_running_style の閾値と同一:
      <=0.20 逃げ / <=0.45 先行 / <=0.70 中団 / それ以外 差追)
指標: 単勝勝率(%) の diff = グループ勝率 - バケット平均勝率
"""
import json
import sqlite3
from collections import defaultdict


def _gate_label(horse_num):
    if horse_num <= 2:  return '内(1-2)'
    if horse_num <= 4:  return '中内(3-4)'
    if horse_num <= 7:  return '中(5-7)'
    if horse_num <= 11: return '中外(8-11)'
    if horse_num <= 14: return '外(12-14)'
    return '大外(15+)'


def _style_label(pos4, num_horses):
    ratio = pos4 / num_horses
    if ratio <= 0.20: return '逃げ'
    if ratio <= 0.45: return '先行'
    if ratio <= 0.70: return '中団'
    return '差追'


def build_gate_style_bias(conn, cutoff_date='2099-01-01',
                           min_n_bucket=100, min_n_group=15):
    """venue×surface×distance 別の枠順・脚質勝率diffテーブルを構築して dict で返す"""

    rows = conn.execute("""
        SELECT venue, surface, distance, horse_num, pos4, num_horses, finish
        FROM results
        WHERE finish IS NOT NULL AND finish < 90 AND finish > 0
          AND horse_num IS NOT NULL AND horse_num > 0
          AND pos4 IS NOT NULL AND pos4 > 0
          AND num_horses IS NOT NULL AND num_horses > 0
          AND venue IS NOT NULL AND surface IS NOT NULL AND distance IS NOT NULL
          AND date < ?
    """, (cutoff_date,)).fetchall()

    if not rows:
        print('  gate_style_bias: データなし')
        return {}

    buckets = defaultdict(list)
    for r in rows:
        key = (r['venue'], r['surface'], int(r['distance']))
        buckets[key].append(r)

    result = {}
    n_buckets = 0
    for (venue, surface, dist), bucket_rows in buckets.items():
        n = len(bucket_rows)
        if n < min_n_bucket:
            continue

        win_total = sum(1 for r in bucket_rows if r['finish'] == 1)
        avg_win_pct = win_total / n * 100

        gate_n  = defaultdict(int)
        gate_win = defaultdict(int)
        style_n  = defaultdict(int)
        style_win = defaultdict(int)

        for r in bucket_rows:
            hit = 1 if r['finish'] == 1 else 0
            g = _gate_label(int(r['horse_num']))
            gate_n[g] += 1
            gate_win[g] += hit
            s = _style_label(r['pos4'], r['num_horses'])
            style_n[s] += 1
            style_win[s] += hit

        gate_out = {}
        for g, gn in gate_n.items():
            if gn < min_n_group:
                continue
            wp = gate_win[g] / gn * 100
            gate_out[g] = {'n': gn, 'win_pct': round(wp, 2),
                            'diff': round(wp - avg_win_pct, 2)}

        style_out = {}
        for s, sn in style_n.items():
            if sn < min_n_group:
                continue
            wp = style_win[s] / sn * 100
            style_out[s] = {'n': sn, 'win_pct': round(wp, 2),
                             'diff': round(wp - avg_win_pct, 2)}

        if not gate_out and not style_out:
            continue

        key_str = f"{venue}_{surface}_{dist}"
        result[key_str] = {
            'venue': venue, 'surface': surface, 'distance': dist,
            'n': n, 'avg_win_pct': round(avg_win_pct, 2),
            'avg_style_wr': round(avg_win_pct, 2),
            'gate': gate_out, 'style': style_out,
        }
        n_buckets += 1

    print(f"  gate_style_bias: {n_buckets}バケット構築 (cutoff={cutoff_date})")
    return result


if __name__ == '__main__':
    import sys
    cutoff = sys.argv[1] if len(sys.argv) > 1 else '2099-01-01'
    out_path = sys.argv[2] if len(sys.argv) > 2 else 'gate_style_bias.json'

    conn = sqlite3.connect('keiba.db')
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.row_factory = sqlite3.Row

    data = build_gate_style_bias(conn, cutoff_date=cutoff)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"  -> {out_path} 保存済み ({len(data)}バケット)")
    conn.close()
