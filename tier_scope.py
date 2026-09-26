# -*- coding: utf-8 -*-
"""tier_scope.py — 会場×表面×距離tier統計(注目レース選出基準 rel/acc)の集計スコープ(2026-09-26新設)。
analyze_mc123_top1_conditions.py / compute_formation_accuracy.py の両方から参照し、開始日を1箇所で管理する。
根拠: 2019年はDB開始直後でコールドスタート(出走馬の40%が過去2走未満、MC123構造テーブル空)、
2020年はMC123構造テーブルが1年分のみ → 2021-01-01が実質的な最古。"""
TIER_START_DATE = "2021-01-01"

# 会場別の開始日上書き(コース特性が不連続、またはデータ品質上除外したい期間がある会場のみ)
TIER_VENUE_START = {
    # 京都: 2020-11-01〜2023-04-21 大規模整備(880億円、芝・ダート路盤改修、レイアウト不変)。改修後のみ。
    # TIER_START_DATE=2021-01-01 の下では自動充足するが、開始日を2019/2020に戻した際に
    # 改修前848レースが混入しないよう明示。
    "京都": "2023-04-22",
    # 阪神: 2024-04-15〜2025-02-28 スタンドリフレッシュ工事で休止(芝65%張替+ダート路盤改修、レイアウト不変)。
    # 2026-09-26調査で前残り率・上がり3Fの変化が対照場の変動幅内のため連続扱い(エントリなし)。
    # リセットしたい場合は "阪神": "2025-03-01" を追加(対象は681レースに減る)。
}


def tier_start_date(venue):
    return TIER_VENUE_START.get(venue, TIER_START_DATE)


def in_tier_scope(venue, race_date):
    return race_date >= tier_start_date(venue)


def scope_label():
    parts = [f"{TIER_START_DATE} 〜 (実行時点)"]
    parts += [f"{v}は{d}〜" for v, d in sorted(TIER_VENUE_START.items())]
    return "; ".join(parts)
