"""
features.py — ボーナス群の共通合算ロジック（りさ案: 特徴量計算の一元化）

背景: backtest_2026.py の score_one_race() と predict_weekend.py の
score_weekend_race() が、同じボーナス群(calc_course_blood_bonus 等)を呼んで
合算する処理をそれぞれ別々に(コピペで)保守していた。この結果、2026-07-21に
backtest側へ追加したcalc_race_level_bonus(rlb)・calc_venue_style_bonus(vstb)・
calc_front4_bonus(f4b)、および以前追加のcalc_family_nicks_bonus(fnkb)が
predict_weekend.py側には反映されておらず、「backtestに足しても本番予想への
反映を忘れる」というリスクが実例として発生していた(2026-07-22 りさ指摘)。

設計方針:
  - compute_all_bonuses() は score_one_race/score_weekend_race の該当ループ内で
    従来 h2['total_score'] = round(h2['total_score'] + bonus + gcbb + ... , 1)
    としていた式を「一字一句同じ左から右への加算順序」でそのまま内包する。
    Python の浮動小数点加算は結合則を満たさないため、項をまとめてから足す
    実装に変えると理論上ビット単位で結果がずれる可能性がある。将来にわたって
    BT数値が完全一致し続けることを保証するため、意図的に元の加算順序を厳守する。
  - 現在デフォルトOFFのフラグ付きボーナス(fnkb/rlb/vstb/f4b)は0.0を返すため、
    この一元化によって predict_weekend.py 側の出力に数値変化は起きない
    (IEEE754で x+0.0 は x と等しい)。フラグが将来ONになった時点で両方に
    同時反映されるようになる、というのがこの一元化の主目的。

呼び出し側で必須のフィールド:
  horse_dict (h2相当の辞書、以下のキーが必要):
    horse_name, horse_num, total_score, _blood_rank, _sire, _dam_sire,
    _prev_pos4, _prev_runs(任意, front4_bonus用。無ければ空リスト扱い)
  race_info (dict):
    date, venue, surface, dist, heads, cond, cushion
"""


def compute_all_bonuses(horse_dict, race_info, sc_conn):
    """全ボーナスを合算し、更新後の total_score とボーナス内訳を返す。

    Args:
        horse_dict: 1頭分のスコアリング中間結果 (dict)。呼び出し元でミューテートしない
                    (新しい total_score は戻り値として返すのみ)。
        race_info: レース共通コンテキスト (dict): date, venue, surface, dist,
                   heads, cond, cushion
        sc_conn: スコアリング用DBコネクション

    Returns:
        (new_total_score: float, breakdown: dict)
        breakdown のキー: course_blood, gate_cond_blood, track_bias, venue_sire,
        venue_damsire, cushion_sire, nicks, family_nicks, race_level,
        venue_style, front4
    """
    # 遅延import: scoring.py の再読み込み(importlib.reload)後でも
    # 呼び出し元(backtest_2026.py/predict_weekend.py)が読み込み済みの
    # scoring モジュールを再利用できるよう、関数内でimportする
    from scoring import (
        calc_course_blood_bonus, calc_gate_cond_blood_bonus, calc_track_bias_bonus,
        calc_venue_sire_bonus, calc_venue_damsire_bonus, calc_cushion_sire_bonus,
        calc_nicks_bonus, calc_family_nicks_bonus, calc_race_level_bonus,
        calc_venue_style_bonus, calc_front4_bonus,
    )

    date    = race_info['date']
    venue   = race_info['venue']
    surf    = race_info['surface']
    dist    = race_info['dist']
    heads   = race_info['heads']
    cond    = race_info['cond']
    cushion = race_info['cushion']

    h2 = horse_dict
    prev_runs = h2.get('_prev_runs') or []

    bonus = calc_course_blood_bonus(h2['horse_name'], date, venue, surf, dist,
                                     h2['_blood_rank'], sc_conn)
    gcbb  = calc_gate_cond_blood_bonus(h2['horse_name'], date, venue, surf, dist,
                                        h2['horse_num'], heads, cond, h2['_sire'], sc_conn)
    tbb   = calc_track_bias_bonus(venue, surf, date, h2['horse_num'], heads,
                                   h2['_prev_pos4'], sc_conn)
    vsb   = calc_venue_sire_bonus(venue, dist, h2['_sire'], sc_conn)
    vdsb  = calc_venue_damsire_bonus(venue, dist, h2['_dam_sire'], sc_conn)
    csb   = calc_cushion_sire_bonus(cushion, h2['_sire'], surf, sc_conn)
    nkb   = calc_nicks_bonus(h2['_sire'], h2['_dam_sire'], surf, sc_conn)
    fnkb  = calc_family_nicks_bonus(h2['_sire'], h2['_dam_sire'], surf, sc_conn)
    rlb   = calc_race_level_bonus(h2['horse_name'], date, sc_conn)
    vstb  = calc_venue_style_bonus(h2['horse_name'], date, venue, surf, dist, sc_conn)
    f4b   = calc_front4_bonus(h2['horse_name'], date, prev_runs)

    # 元の score_one_race()/score_weekend_race() と一字一句同じ左→右の加算順序
    # (h2['total_score'] を先頭に、以降 bonus,gcbb,tbb,vsb,vdsb,csb,nkb,fnkb,rlb,vstb,f4b)
    new_total_score = round(
        h2['total_score'] + bonus + gcbb + tbb + vsb + vdsb + csb + nkb + fnkb + rlb + vstb + f4b, 1
    )

    breakdown = {
        'course_blood':    bonus,
        'gate_cond_blood': gcbb,
        'track_bias':      tbb,
        'venue_sire':      vsb,
        'venue_damsire':   vdsb,
        'cushion_sire':    csb,
        'nicks':           nkb,
        'family_nicks':    fnkb,
        'race_level':      rlb,
        'venue_style':     vstb,
        'front4':          f4b,
    }
    return new_total_score, breakdown
