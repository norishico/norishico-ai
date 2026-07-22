"""
counterfactual_bonus_audit.py — 汎用ボーナスON/OFF反実仮想監査フレームワーク

背景: cushion_sire_bonus/nicks_bonus/H3(front4)/M1(softmax温度)の監査で、「記録上は
採用済みのボーナスが実際には本番のバックテスト結果に一切影響していない」というケースが
複数見つかった(2026-07-21〜22)。3件のセカンドオピニオン(Fable5/ChatGPT/Gemini)が
共通して「ON/OFFを機械的に確認する反実仮想テストの仕組みを、まだ監査していない残り全ての
ボーナスにも適用すべき」と指摘したことを受け、再利用可能な汎用スクリプトとして実装する。

使い方:
    python counterfactual_bonus_audit.py --year 2025
    python counterfactual_bonus_audit.py --year 2025 --tier1-only  (高速: 発火率のみ)

設計:
  Tier1 (発火率監査、全ボーナス同時、1回のBTパスで完結・高速):
    各ボーナス関数をカウンタ付きラッパーでモンキーパッチし、通常のBTを1回実行。
    「呼び出し回数」「非ゼロを返した回数」を記録する。非ゼロ0件なら即座に「死亡疑い」。

  Tier2 (実際の採否・買い目への影響、ボーナスごとに個別のBT再実行):
    対象ボーナス1つだけを強制的に0を返すようモンキーパッチし、フルBTを再実行して
    baseline(全ボーナスON)とbet_records/ROIを比較する(cushion_sire_bonus等で
    確立済みの手法と同一)。Tier1で非ゼロ率が高いボーナスほど優先的に実施。

新しいボーナスを追加した場合: BONUS_REGISTRY に1行追加するだけで監査対象に含められる
(scoring.py内の関数名と、features.compute_all_bonusesのbreakdownキー名を指定する)。
"""
import argparse
import sys
import os
import time
import shutil
import sqlite3
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import backtest_v6 as bt6
import scoring

# ── 監査対象レジストリ: (scoring.py内の関数名, features.pyのbreakdownキー) ──
# 追加時はここに1行足すだけでよい。
BONUS_REGISTRY = [
    ('calc_course_blood_bonus', 'course_blood'),
    ('calc_gate_cond_blood_bonus', 'gate_cond_blood'),
    ('calc_track_bias_bonus', 'track_bias'),
    ('calc_venue_sire_bonus', 'venue_sire'),
    ('calc_venue_damsire_bonus', 'venue_damsire'),
    ('calc_family_nicks_bonus', 'family_nicks'),
]

WINSORIZE_CAP = 50000


def _fresh_tmp_db(tag, year, src_db='keiba.db'):
    tmp_db = f'keiba_tmp_cfaudit_{tag}_{year}.db'
    if Path(tmp_db).exists():
        Path(tmp_db).unlink()
    if Path(f'{src_db}-wal').exists():
        c = sqlite3.connect(src_db)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
    shutil.copy2(src_db, tmp_db)
    return tmp_db


def _run_year(year, tmp_db):
    all_races, bet_records = bt6.run_year_v6(year, tmp_db)
    for b in bet_records:
        if 'cost' not in b:
            b['cost'] = 1000
    total_inv = sum(b['cost'] for b in bet_records)
    total_ret = sum(b['ret'] for b in bet_records)
    total_prof = int(total_ret - total_inv)
    total_roi = total_ret / total_inv * 100 if total_inv else 0
    win_ret = sum(min(b['ret'], WINSORIZE_CAP) for b in bet_records)
    win_roi = win_ret / total_inv * 100 if total_inv else 0
    return {
        'n_bet': len(bet_records), 'investment': total_inv, 'profit': total_prof,
        'roi': round(total_roi, 1), 'winsorized_roi': round(win_roi, 1),
        'bet_records': bet_records,
    }


def tier1_firing_rates(year):
    """全ボーナスを同時にカウンタ付きラッパーでモンキーパッチし、1回のBTで発火率を集計する。"""
    print(f"\n{'='*70}\nTier1: 発火率監査 ({year}年、全ボーナス同時計測)\n{'='*70}")

    counters = {}
    originals = {}
    for fn_name, _ in BONUS_REGISTRY:
        orig = getattr(scoring, fn_name)
        originals[fn_name] = orig
        counters[fn_name] = {'calls': 0, 'nonzero': 0, 'examples': []}

        def make_wrapper(name, orig_fn):
            def _wrapped(*args, **kwargs):
                c = counters[name]
                c['calls'] += 1
                v = orig_fn(*args, **kwargs)
                if v and v != 0.0:
                    c['nonzero'] += 1
                    if len(c['examples']) < 5:
                        c['examples'].append((args[:3], v))
                return v
            return _wrapped

        setattr(scoring, fn_name, make_wrapper(fn_name, orig))

    tmp_db = _fresh_tmp_db('tier1', year)
    t0 = time.time()
    result = _run_year(year, tmp_db)
    elapsed = time.time() - t0
    Path(tmp_db).unlink(missing_ok=True)

    # 元に戻す
    for fn_name, orig in originals.items():
        setattr(scoring, fn_name, orig)

    print(f"  BT完了: n_bet={result['n_bet']} roi={result['roi']}% ({elapsed:.0f}s)")
    print(f"\n  {'ボーナス名':<28}{'呼出回数':>10}{'非ゼロ':>10}{'非ゼロ率':>10}  判定")
    tier1_out = {}
    for fn_name, bd_key in BONUS_REGISTRY:
        c = counters[fn_name]
        rate = c['nonzero'] / c['calls'] * 100 if c['calls'] else 0
        verdict = '【死亡疑い】非ゼロ0件' if c['nonzero'] == 0 else 'OK(発火あり)'
        print(f"  {fn_name:<28}{c['calls']:>10}{c['nonzero']:>10}{rate:>9.2f}%  {verdict}")
        tier1_out[fn_name] = {'calls': c['calls'], 'nonzero': c['nonzero'], 'rate_pct': round(rate, 3),
                               'suspect_dead': c['nonzero'] == 0, 'examples': c['examples']}
    return tier1_out, result


def tier2_ablation(year, baseline_result, targets):
    """targetsで指定したボーナスを1つずつ強制的に0にしてフルBTを再実行し、baselineと比較する。"""
    print(f"\n{'='*70}\nTier2: 実際の採否・買い目への影響監査 ({year}年、対象={len(targets)}件)\n{'='*70}")

    tier2_out = {}
    for fn_name, bd_key in targets:
        orig = getattr(scoring, fn_name)

        def _zero(*args, **kwargs):
            return 0.0

        setattr(scoring, fn_name, _zero)
        tmp_db = _fresh_tmp_db(f'tier2_{fn_name}', year)
        t0 = time.time()
        result = _run_year(year, tmp_db)
        elapsed = time.time() - t0
        Path(tmp_db).unlink(missing_ok=True)
        setattr(scoring, fn_name, orig)

        n_diff = result['n_bet'] - baseline_result['n_bet']
        profit_diff = result['profit'] - baseline_result['profit']
        roi_diff = round(result['roi'] - baseline_result['roi'], 1)
        winroi_diff = round(result['winsorized_roi'] - baseline_result['winsorized_roi'], 1)

        # bet_recordsレベルでの差分(honmei_name/dateキーで突合)
        base_keys = {(b['date'], b['venue'], b['race_num']): b for b in baseline_result['bet_records']}
        abl_keys = {(b['date'], b['venue'], b['race_num']): b for b in result['bet_records']}
        changed_bets = 0
        for k in set(base_keys) | set(abl_keys):
            bb, ab = base_keys.get(k), abl_keys.get(k)
            if (bb is None) != (ab is None):
                changed_bets += 1
            elif bb is not None and ab is not None:
                if bb.get('honmei_name') != ab.get('honmei_name') or bb.get('buy_zone') != ab.get('buy_zone'):
                    changed_bets += 1

        verdict = '影響なし(baselineと完全一致)' if (n_diff == 0 and profit_diff == 0) else \
                  f'影響あり(買い目変化{changed_bets}件)'
        print(f"\n  {fn_name}: baseline n={baseline_result['n_bet']} -> ablated n={result['n_bet']} "
              f"(diff={n_diff:+d})  profit_diff={profit_diff:+d}  roi_diff={roi_diff:+.1f}pt  "
              f"win_roi_diff={winroi_diff:+.1f}pt  買い目変化={changed_bets}件  ({elapsed:.0f}s)")
        print(f"    -> {verdict}")

        tier2_out[fn_name] = {
            'baseline_n': baseline_result['n_bet'], 'ablated_n': result['n_bet'], 'n_diff': n_diff,
            'profit_diff': profit_diff, 'roi_diff': roi_diff, 'winsorized_roi_diff': winroi_diff,
            'changed_bets': changed_bets,
        }
    return tier2_out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--year', type=int, default=2025)
    ap.add_argument('--tier1-only', action='store_true')
    args = ap.parse_args()

    import json
    tier1_out, baseline_result = tier1_firing_rates(args.year)

    tier2_out = {}
    if not args.tier1_only:
        tier2_out = tier2_ablation(args.year, baseline_result, BONUS_REGISTRY)

    print(f"\n{'='*70}\n=== 総合サマリ ({args.year}年) ===\n{'='*70}")
    for fn_name, _ in BONUS_REGISTRY:
        t1 = tier1_out[fn_name]
        line = f"  {fn_name:<28} 発火率={t1['rate_pct']:6.2f}%"
        if fn_name in tier2_out:
            t2 = tier2_out[fn_name]
            line += f"  買い目件数diff={t2['n_diff']:+d}  ROI diff={t2['roi_diff']:+.1f}pt  変化{t2['changed_bets']}件"
        if t1['suspect_dead']:
            line += '  【死亡疑い】'
        print(line)

    out_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f'counterfactual_bonus_audit_{args.year}_result.json'
    )
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({'year': args.year, 'tier1': tier1_out, 'tier2': tier2_out,
                    'baseline_summary': {k: v for k, v in baseline_result.items() if k != 'bet_records'}},
                   f, ensure_ascii=False, indent=2, default=str)
    print(f"\n結果を保存: {out_path}")


if __name__ == '__main__':
    main()
