"""
mc123_engine.py — MCエンジン本体 (Phase 2)

run_mc_fixed(generate_mc_record.py)・run_mc_lite(backtest_sim_lite.py)の複製ではなく、
CTA(build_class_par.py)・個体化ペース応答/Grit(build_pace_baseline.py)・風スロットを
組み込んだ完全新規の独立エンジン。馬ごとに{p1,p2,p3,ptop3}を出力する。

【まだ本番未統合の独立ライン】v6.6への統合・表示ページ(mc123.html)・オッズフィルタ・
正式BT/WF CVはすべてスコープ外(Phase 2は「動く版」を作ることが目的)。

【2026-07-20 正式較正済み(第2回、rfa_rank_z/rfa_margin_z/l3f_z追加後)】
K_ABILITY・GRIT_SCALE・WIND_A_PACE_SHIFT・WIND_B_STYLE・WIND_C_GUST・K_RANK・K_MARGIN・
K_L3Fの8係数を、calibrate_mc123.py/run_calibration.pyによりBrier score最小化の座標降下法
で較正済み(探索: 2021-2022年データのみ、n_mc=200、49回評価、625.0秒。8係数化に伴い
既存5係数の候補数を5→3に削減して探索コストを制御)。

【採否判定のbaseline訂正について(重要)】初回報告時、較正前後比較の"before"に
「新3特徴量を較正せずplaceholder=1.0のまま足した状態」を誤って使い、相対改善0.356%
(閾値0.44%未達)として一旦「不採用」と報告した。コーディネーターの指摘により、正しい
比較対象は「新3特徴量を全く含まない状態(=この較正の前の本番モデル、K_RANK=K_MARGIN=
K_L3F=0相当)」であるべきと判明。正しいbaseline(pooled avg_brier=0.0658723、
年別2023=0.065914/2024=0.066176/2025=0.065527)と比較すると:
  pooled相対改善 = (0.065872-0.064931)/0.065872 = 1.43%(閾値0.44%を大きく上回る)
  年別: 2023=1.60% / 2024=1.30% / 2025=1.39%(3年とも個別改善かつ前回実績0.22%を大幅に上回る)
この訂正後の数値で事前登録の採否基準を明確にクリアしたため、本反映に至った。
詳細はcalibration_result.json参照。

【2026-09-20 脚質較正2件(①脚質×距離較正項/③K_LOWINFO)較正実施】
脚質別の全馬較正診断で見つかった誤較正(短距離の逃げ過小/追込過大、長距離の先行過大/
追込過小、重馬場の先行過大、低情報馬の過小評価)への対応。①(K_NIGE_LEVEL等5係数)は
Track A・Track Bいずれの事前登録基準も未達で不採用(係数は現行値のまま、詳細は各定数
直上のコメント参照)。③(K_LOWINFO)はTrack B(較正修正)基準を余裕を持ってクリアし
K_LOWINFO=3.0で採用(mc123_bt_results_v2.json再生成済み)。詳細は各定数直上のコメント、
style_terms_calibration_result.json / lowinfo_calibration_result.json参照。

【2026-09-20 track_cond"不"判定漏れ修正】heavy/wet判定がDB主流の1文字表記"不"を
拾えていなかった(2文字表記"不良"のみ一致)ため、normalize_track_cond()で正規化。
"""
import hashlib
import math
import sqlite3
import numpy as np

import generate_race_sim as gsim
from build_class_par import (
    build_class_par_table, calibrate_k_cls, load_same_day_bias_map, compute_horse_cta,
)
from build_pace_baseline import (
    build_baseline_table, compute_horse_pgr, compute_horse_pace_response,
)

DB_PATH = "keiba.db"
N_MC_DEFAULT = 500  # 動作確認用。本番2000回はn_mc引数で切替可能

# COURSE_ADV: run_mc_fixed(generate_mc_record.py:119-120)から継承
# 東京2600m/中山3390m/中山3110mはDB照合の結果、該当レース0件（JRAに実在しない距離）のため削除(2026-07-20)
COURSE_ADV = {'阪神': {1800: 3}, '中京': {1800: 3, 2000: 3}}

# ── CTA項の係数(2026-07-20較正済み。座標降下法でplaceholder=1.0のまま最良と判定) ──
K_ABILITY = 1.0  # cta_z * K_ABILITY

# ── rfa_rank_z / rfa_margin_z / l3f_z 係数(2026-07-20 第2回較正で正式反映) ──
# K_RANK=K_MARGIN=2.0で共に生存(同水準)。プランナーの事前スクリーニング
# (「着順ベース・着差ベース両方の地力情報が必要」という予測)と整合する結果。
K_RANK = 2.0
K_MARGIN = 2.0
K_L3F = 1.0  # placeholderのまま最良と判定(変化なし)

# ── K_LAYOFF(鮮度/休み明けペナルティ、2026-07-20 プランナー設計) ──
# g -= K_LAYOFF * layoff_pen。layoff_penは較正テーブル不要の固定バケット
# (mc123_batch.compute_layoff_pen_fast: <=90日=0.0 / <=180日=1.0 / それ以上=2.7)。
#
# 【2026-07-20 較正実施→不採用】座標降下法で探索した結果K_LAYOFF=0.8が最良候補として
# 見つかったが、OOS評価(2023/2024/2025、before=K_LAYOFF=0の現行本番モデル)でpooled
# 相対Brier改善は+0.16%(全年個別には改善したが、事前登録基準0.44%に量が届かず)。
# プランナー事前予測(0.06〜0.2%)の範囲内に収まる結果で、不採用と判定した。
# K_LAYOFF=0.0としてgainへの影響を完全に無効化し、この機能導入前と数学的に同一の
# 挙動に戻す。layoff_pen自体の計算ロジック(mc123_batch.compute_layoff_pen_fast)は
# 将来の再検討用にコードとして残置する。
K_LAYOFF = 0.0

# ── Grit項のスケール(2026-07-20 第2回較正。10.0 -> 5.0) ─────────
# Grit_H/Grit_Sはrel_finish残差(±0.25キャップ、0-1スケール)なので、gainのpoint
# スケールに変換する係数。
GRIT_SCALE = 5.0

# ── 風スロット係数(2026-07-20 第2回較正。W1・W2は0まで押し下げ) ──
# 【重要】rfa_rank_z/rfa_margin_z/l3f_z(地力系特徴量)投入後、W1・W2は共に0.0に較正された。
# これは「僅差で効果が小さい」のではなく、Brier最小化の観点で風のペースシフト効果・
# 脚質ボーナス効果の最適推定値が文字通りゼロになったことを意味する(地力系特徴量が
# 入った時点で風の実力差別化への寄与は識別できなくなった、というより強いnull result)。
# この帰結として、Gate2 F6フィルタの「風による当日実況/中立シナリオのP1差」も
# 定義上ほぼゼロになり、F6条件が構造的に発火しなくなる(n=0の再確認はこの意味で
# 「僅差の不合格」ではなく「風シフト自体が較正でゼロになったことの必然的帰結」)。
WIND_A_PACE_SHIFT = 0.0
WIND_B_STYLE = 0.0
# W3: 突風ノイズ拡大。noise_std = BASE_NOISE_STD + WIND_C_GUST * gust_max。
# 0.15のまま変化なし(ノイズ拡大効果はW1/W2と異なり生き残った)。
BASE_NOISE_STD = 5.0
WIND_C_GUST = 0.15

# ── K_WET_APT(個体別道悪適性、2026-08-01 新規事前登録テスト→不採用) ──
# g += K_WET_APT * wet_apt_z(道悪レース(稍/重/不良)の時のみ加算)。
# wet_apt_z = 縮約道悪複勝率 - 縮約良馬場複勝率(test_wet_aptitude_residual.py参照)。
#
# 【2026-08-01 検証実施→不採用】train(2021-2022)グリッド探索でK_WET_APT=2.0が
# 最良候補として見つかったが、OOS評価(2023/2024/2025、道悪レース限定プール)で
# 相対Brier改善は-0.012%(悪化、3年中1年のみ僅かにプラス+0.008%)、事前登録基準
# 0.44%に届かず不採用。母集団の道悪複勝率(21.81%)と良馬場複勝率(21.81%)が
# cutoff時点で完全一致しており、個体の「道悪適性」という発想自体、CTA/RFA/L3Fが
# 既に地力情報を織り込んだ後には残差として検出できないと判明。
# K_WET_APT=0.0のままgainへの影響を完全に無効化し、導入前と数学的に同一の挙動を維持。
K_WET_APT = 0.0

# ── 脚質×距離較正項(2026-09-20 追加。較正前は全て現行相当=挙動不変) ──
# 背景: 脚質別の全馬較正診断(project_ayokeiba_mc123.md/rules/ayokeiba-mc.md参照)で、
# 短距離(ダ1200/芝1200)の逃げ過小評価・追込過大評価、芝2400m以上の先行過大評価・
# 追込過小評価、重不良馬場の先行過大評価(較正比0.87)が確認された。この5係数は
# calibrate_style_terms.py(座標降下法、train=2021-2022、既存8係数は凍結)で較正した。
#
# 【2026-09-20 較正実施→不採用】座標降下法(train=2021-2022, n_mc=200, 23回評価)で
# K_NIGE_LEVEL=0.5 / K_OI_LEVEL=0.0 / K_SDIST_FRONT=3.0 / K_SDIST_CLOSER=3.0 /
# HEAVY_SENKO=0.3 が最良候補として見つかったが、OOS評価(2023/2024/2025、n_mc=500、
# MCノイズ床 pooled eps_Brier(win)=0.000003)で以下の通りTrack A・Track Bいずれの
# 事前登録基準も未達:
#   Track A(精度改善、閾値0.44%): pooled相対Brier(win)改善=0.054%で大幅未達
#     (年別: 2023=+0.130% / 2024=+0.041% / 2025=-0.010%)
#   Track B(較正修正、閾値=D pooledで40%以上減少 かつ3年中2年以上で減少):
#     非劣化条件は満たす(pooled Brier(win) -0.054%・Brier(place) -0.058%、全年改善
#     または誤差範囲内の悪化)が、較正比D(脚質xsurfacex距離帯、n>=300セル)の
#     pooled削減率は35.1%(閾値40%に未達)。年別では2023=37.1%・2025=31.2%減少した
#     一方、2024年はD自体が34.7%悪化(0.00818->0.01101)しており、年別頑健性にも
#     疑義が残る(3年中2年減少という数のみは満たすが、pooled閾値未達のため不採用)。
#     最大|ln較正比|は0.4165->0.3746に減少。top1交代率11.84%。
# 詳細はstyle_terms_calibration_result.json参照(スクラッチパッド)。
# 5係数=0(HEAVY_SENKOのみ0.6)のまま、gainへの影響を完全に無効化し、この機能導入前
# と数学的に同一の挙動に戻す。
K_NIGE_LEVEL = 0.0     # 逃げのレベル項(距離非依存)。g += K_NIGE_LEVEL (st=="逃げ")
K_OI_LEVEL = 0.0       # 追い込みのレベル項。g += K_OI_LEVEL (st=="追い込み")
K_SDIST_FRONT = 0.0    # 前脚質の距離勾配: g += K_SDIST_FRONT * s * ln(1600/dist), s=逃げ1.0/先行0.5
K_SDIST_CLOSER = 0.0   # 後脚質の距離勾配: g -= K_SDIST_CLOSER * s * ln(1600/dist), s=追い込み1.0/差し0.5
HEAVY_SENKO = 0.6      # 重馬場の先行ボーナス係数(既存リテラル0.6の係数化。逃げ1.0/差し追込-0.5は不変)

# ── K_LOWINFO(低情報馬の事前平均項、2026-09-20 追加・較正・採用) ──
# 対象: 有効過去走(rfa_rank_z算出に使う、finish/num_horses>1が揃った過去走)<2 の馬
# (mc123_batch.precompute_horse_features_fastが"low_info"フラグとして計算)。
# 【事前診断による絞り込み】当初案は「n_runs_cta==0 または有効過去走<2」だったが、
# n_runs_cta==0単独条件はCTA計算ギャップ(class_par参照失敗等)によりベテラン馬
# (有効過去走10+)を誤って12.5%混入させることが判明したため削除し、この条件のみとした
# (lowinfo_diagnosis.py、2024年サンプル20頭のresultsテーブル照合で確認)。
#
# 【2026-09-20 較正実施→Track B(較正修正)で採用】①の係数固定(不採用のため全て
# デフォルト)の下、K_LOWINFOを座標降下法(train=2021-2022, n_mc=200, 候補{0,1,2,3})
# で較正した。Brier(win)がK=0から3まで単調改善したため候補上限のK=3.0を採用
# (グリッド上限での選択であり、より大きな値の余地は今回の事前登録範囲外)。
# OOS評価(2023/2024/2025、n_mc=500、CRN)結果:
#   Track A(閾値0.44%): pooled相対Brier(win)改善=0.163%で未達(年別+0.10/+0.23/+0.16%)
#   Track B(閾値: 較正比D=ln(win_rate/mean_p1)^2 が pooledで40%以上減少 かつ
#     3年中2年以上で減少): 非劣化(pooled Brier(win) -0.163%改善・Brier(place)も
#     3年とも改善、悪化なし)を満たした上で、low_info集団の較正比(実勝率/予測p1)が
#     pooled 2.979->1.591 (D=1.191->0.216、削減率81.9%)、年別も2023=90.8%・
#     2024=74.7%・2025=82.1%減少と3年とも大幅減少。max|ln比|も1.09->0.47に減少。
#     Track B基準を余裕を持ってクリアしたため採用。top1交代率0.69%(低頻度)。
#   (①のD定義は脚質x surface x距離帯の多セル集計だったが、③は低情報馬という単一
#   セグメントが対象のため、同じln(比)^2の考え方をこの1セグメントに適用した。)
# 詳細はlowinfo_calibration_result.json / lowinfo_diagnosis.json参照(スクラッチパッド)。
K_LOWINFO = 3.0

# 【2026-09-20 track_cond"不"判定漏れ修正】DBの馬場状態コードは1文字表記(良/稍/重/不)
# が主流。JV-Link等一部データのみ2文字表記(不良/稍重)で来ることがあるため、
# 別名として正規化してから判定する。
_TC_ALIAS = {"不良": "不", "稍重": "稍"}


def normalize_track_cond(tc):
    """DB主流の1文字表記(良/稍/重/不)に正規化。2文字表記(不良/稍重、JV-Link・
    this_week_races由来)は別名として受理。"""
    if not tc:
        return "良"
    return _TC_ALIAS.get(tc, tc)


def hash64_seed(race_id: str) -> int:
    """race_idから決定的な32bit seedを生成する(PYTHONHASHSEED依存のbuiltin hash()は
    使わない — 環境変数固定でも将来のPython版で挙動が変わりうるため、hashlibで固定)。"""
    h = hashlib.sha256(race_id.encode("utf-8")).hexdigest()
    return int(h[:16], 16) % (2 ** 32)


def _style_group(style):
    if style in ("逃げ", "先行"):
        return "front"
    return "closer"


def precompute_horse_features(conn, horses, race_info, class_par, k_cls, same_day_bias_map,
                               pace_baseline):
    """MC試行ループの外で1回だけ計算する馬ごとの静的特徴量
    (CTA, v_i(P), Grit_H, Grit_S)をhorsesの各dictに追加して返す。"""
    asof_date = race_info.get("date")
    distance = race_info.get("distance", 1600)
    surface = race_info.get("surface", "芝")

    for h in horses:
        name = h["horse_name"]
        cta = compute_horse_cta(conn, name, asof_date, class_par, k_cls, same_day_bias_map)
        h["cta_z"] = cta["cta_main"] if cta["cta_main"] is not None else 0.0

        pgr = compute_horse_pgr(conn, name, asof_date, pace_baseline)
        h["grit_h"] = pgr["grit_h"]
        h["grit_s"] = pgr["grit_s"]

        resp = compute_horse_pace_response(conn, name, asof_date, distance, surface,
                                            jockey=h.get("jockey", ""))
        h["pace_v"] = resp["v"]
        if "style" not in h or not h["style"]:
            h["style"] = resp["style"]
        # 【2026-07-20 時間制約による簡略化】rfa_rank_z/rfa_margin_z/l3f_zはDBクエリ版では
        # 未実装(run_mc123側は.get(...,0.0)で中立0にフォールバックするため動作は壊れない)。
        # 高速一括版(mc123_batch.precompute_horse_features_fast)側には実装済みで、
        # 較正・BTはすべてそちらを使う。_self_test()専用のこの経路は将来必要になれば
        # mc123_batch.compute_rank_z_fast等をDB版に移植すること。
    return horses


def run_mc123(horses, race_info, n_mc=N_MC_DEFAULT, seed=None, wind=None):
    """MC本体。horsesは事前にprecompute_horse_features()済みのリストを渡すこと。

    Args:
        horses: list[dict] (horse_name, umaban, style, gate, cta_z, grit_h, grit_s, pace_v)
        race_info: dict (venue, distance, track_cond, num_horses, date, surface)
        n_mc: 試行回数
        seed: Noneならrace_idから決定的に生成
        wind: {'tail_home': float, 'gust_max': float} or None(中立シナリオ)

    Returns:
        list[dict]: horses と同じ順序で {'p1','p2','p3','ptop3'}
    """
    n = len(horses)
    if n < 3:
        return [{"p1": 1.0 / n, "p2": 1.0 / n, "p3": 1.0 / n, "ptop3": 1.0} for _ in range(n)]

    if seed is None:
        seed = hash64_seed(race_info.get("race_id", "unknown"))
    rng = np.random.default_rng(seed)

    tc = normalize_track_cond(race_info.get("track_cond", "良"))
    venue = race_info.get("venue", "")
    dist = race_info.get("distance", 1600)
    c_adj = COURSE_ADV.get(venue, {}).get(dist, 0)
    heavy = tc in ("重", "不")
    hv = 3.0 if tc == "不" else 2.0
    wet = tc in ("稍", "重", "不")  # K_WET_APT用。heavyより広い(稍を含む)
    n_nige = sum(1 for h in horses if h["style"] == "逃げ")
    n_front = sum(1 for h in horses if h["style"] in ("逃げ", "先行"))
    num_h = race_info.get("num_horses", n)

    # ── 脚質×距離較正項用の距離依存項(レース単位で1回だけ計算) ──
    ln_dist = math.log(1600.0 / max(800, dist))  # 1200m:+0.29 / 1600:0 / 2000:-0.22 / 2400:-0.41 / 3000:-0.63
    _S_FRONT = {"逃げ": 1.0, "先行": 0.5}
    _S_CLOSER = {"追い込み": 1.0, "差し": 0.5}

    pH, pM, pS = 0.25, 0.40, 0.35
    if n_nige >= 3:
        adj = min(0.20, 0.08 * (n_nige - 1))
        pH = min(0.55, pH + adj); pS = max(0.10, pS - adj * 0.7); pM = max(0.20, pM - adj * 0.3)
    elif n_nige == 2:
        pH = min(0.50, pH + 0.08); pS = max(0.15, pS - 0.05)
    elif n_nige == 0:
        pH = max(0.05, pH - 0.10); pS = min(0.55, pS + 0.10)
    if n > 0 and n_front / n > 0.50:
        pH = min(0.50, pH + 0.05); pS = max(0.10, pS - 0.05)
    if num_h >= 17:
        pH = min(0.55, pH + 0.12); pS = max(0.10, pS - 0.09)
    elif num_h >= 13:
        pH = min(0.50, pH + 0.08); pS = max(0.10, pS - 0.06)
    elif num_h <= 8:
        pH = max(0.05, pH - 0.05); pS = min(0.55, pS + 0.04)
    inner = any(h.get("gate", 5) <= 4 and h["style"] in ("逃げ", "先行") for h in horses)
    outer = any(h.get("gate", 5) >= 5 and h["style"] in ("逃げ", "先行") for h in horses)
    if inner and outer:
        pH = min(0.55, pH + 0.03); pS = max(0.10, pS - 0.03)

    # ── W1: 風によるペース確率補正 ──
    if wind and wind.get("tail_home") is not None:
        th = wind["tail_home"]
        shift = WIND_A_PACE_SHIFT * th  # 正th(追風)→Sへシフト、負(向風)→Hへシフト
        pH = max(0.05, pH - shift)
        pS = max(0.05, pS + shift)

    t = pH + pM + pS
    pH /= t; pM /= t; pS /= t

    # ── W3: 突風によるノイズ拡大 ──
    noise_std = BASE_NOISE_STD
    if wind and wind.get("gust_max") is not None:
        noise_std = BASE_NOISE_STD + WIND_C_GUST * wind["gust_max"]

    tail_home = wind.get("tail_home", 0.0) if wind else 0.0

    rank_counts = np.zeros((3, n))  # [0]=1着, [1]=2着, [2]=3着

    for _ in range(n_mc):
        P = rng.choice(["H", "M", "S"], p=[pH, pM, pS])
        gain = np.zeros(n)
        for i, h in enumerate(horses):
            st = h["style"]
            # ── 個体化ペース応答 (STYLE_DEF静的値の代わりにv_i(P)を使用) ──
            v = h.get("pace_v", {}).get(P, 60)
            # ── CTA項 (last3f項の代替。上位互換のため削除) + rfa_rank_z/rfa_margin_z/l3f_z(2026-07-20追加) ──
            g = (75.0 * v / 100 - 70) * 0.45 + h.get("cta_z", 0.0) * K_ABILITY \
                + h.get("rfa_rank_z", 0.0) * K_RANK + h.get("rfa_margin_z", 0.0) * K_MARGIN \
                + h.get("l3f_z", 0.0) * K_L3F \
                - K_LAYOFF * h.get("layoff_pen", 0.0)

            # ── 脚質×距離較正項(2026-09-20追加、係数0でidentity) ──
            if st == "逃げ":
                g += K_NIGE_LEVEL
            elif st == "追い込み":
                g += K_OI_LEVEL
            g += K_SDIST_FRONT * _S_FRONT.get(st, 0.0) * ln_dist - K_SDIST_CLOSER * _S_CLOSER.get(st, 0.0) * ln_dist
            if h.get("low_info"):
                g += K_LOWINFO

            bon = 0.0
            if P == "S":
                if st == "逃げ":
                    bon = 2.0
                elif st == "先行":
                    bon = 0.5

            # ── シナリオ条件付きGrit: MC試行が引いたPに応じてのみ加算 ──
            if P == "H":
                bon += h.get("grit_h", 0.0) * GRIT_SCALE
            elif P == "S":
                bon += h.get("grit_s", 0.0) * GRIT_SCALE

            if heavy:
                if st == "逃げ":
                    bon += hv
                elif st == "先行":
                    bon += hv * HEAVY_SENKO
                elif st in ("差し", "追い込み"):
                    bon -= hv * 0.5
            if wet:
                bon += K_WET_APT * h.get("wet_apt_z", 0.0)
            if c_adj > 0 and st in ("逃げ", "先行"):
                bon += c_adj * 0.5
            elif c_adj < 0 and st in ("差し", "追い込み"):
                bon += abs(c_adj) * 0.5

            gate = h.get("gate", 4)
            if gate >= 6 and st in ("逃げ", "先行"):
                if any(hh.get("gate", 4) <= 4 and hh["style"] in ("逃げ", "先行")
                       for j2, hh in enumerate(horses) if j2 != i):
                    if P == "H":
                        g -= 1.0

            # ── W2: 脚質×直線風ボーナス ──
            if tail_home:
                if _style_group(st) == "front":
                    bon -= WIND_B_STYLE * tail_home  # 追風=front不利
                else:
                    bon += WIND_B_STYLE * tail_home  # 追風=closer有利

            gain[i] = g + bon

        noise = rng.normal(0, noise_std, n)
        order = np.argsort(-(gain + noise))
        for pos in range(min(3, n)):
            rank_counts[pos, order[pos]] += 1

    out = []
    for i in range(n):
        p1 = rank_counts[0, i] / n_mc
        p2 = rank_counts[1, i] / n_mc
        p3 = rank_counts[2, i] / n_mc
        out.append({"p1": round(p1, 4), "p2": round(p2, 4), "p3": round(p3, 4),
                     "ptop3": round(p1 + p2 + p3, 4)})
    return out


def _self_test():
    """this_week_races.jsonの実データ1レースで動作確認する。"""
    import json
    import time
    from pathlib import Path

    p = Path("this_week_races.json")
    if not p.exists():
        print("this_week_races.json が見つかりません。DBから直近レースで代替テストします。")
        return _self_test_from_db()

    races = json.load(open(p, encoding="utf-8"))
    race = next((r for r in races if r.get("horses") and len(r["horses"]) >= 5), None)
    if race is None:
        print("有効なレースが見つかりません。DBから代替テストします。")
        return _self_test_from_db()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = None
    print("=== 事前テーブル読み込み ===")
    t0 = time.time()
    class_par = build_class_par_table(conn, verbose=False)
    k_cls = calibrate_k_cls(conn, verbose=False)
    bias_map = load_same_day_bias_map(conn)
    pace_baseline = build_baseline_table(conn, verbose=False)
    print(f"  {time.time()-t0:.1f}秒")

    venue = race.get("venue", "")
    dist = race.get("distance") or 1600
    srf = race.get("surface", "芝")
    date = race.get("date")
    horses = []
    for hi, h in enumerate(race.get("horses", [])):
        hn = (h.get("name", "") or "").strip()
        if not hn:
            continue
        uma = h.get("umaban") or (hi + 1)
        horses.append({
            "horse_name": hn, "umaban": uma, "jockey": (h.get("jockey", "") or "").strip(),
            "gate": min(8, (uma - 1) // 2 + 1), "style": None,
        })

    race_info = {"venue": venue, "distance": dist, "track_cond": "良",
                 "num_horses": len(horses), "date": date, "surface": srf,
                 "race_id": f"{date}_{venue}_{race.get('race_num', 1)}"}

    print(f"\n=== テスト対象: {date} {venue} {race.get('race_num')}R {srf}{dist}m "
          f"({len(horses)}頭) ===")

    t0 = time.time()
    horses = precompute_horse_features(conn, horses, race_info, class_par, k_cls, bias_map,
                                        pace_baseline)
    t_feat = time.time() - t0

    # シナリオ1: 当日実況(簡易的に会場代表的な風値を仮定。実データJOINは今回省略)
    wind_actual = {"tail_home": 1.5, "gust_max": 8.0}
    t0 = time.time()
    result_actual = run_mc123(horses, race_info, n_mc=N_MC_DEFAULT, wind=wind_actual)
    t_mc_actual = time.time() - t0

    # シナリオ2: 中立(風なし・良馬場)
    t0 = time.time()
    result_neutral = run_mc123(horses, race_info, n_mc=N_MC_DEFAULT, wind=None)
    t_mc_neutral = time.time() - t0

    print(f"\n特徴量事前計算: {t_feat:.2f}秒 ({len(horses)}頭)")
    print(f"MC実行(当日実況シナリオ, n_mc={N_MC_DEFAULT}): {t_mc_actual:.2f}秒")
    print(f"MC実行(中立シナリオ, n_mc={N_MC_DEFAULT}): {t_mc_neutral:.2f}秒")

    print(f"\n{'馬名':<14}{'style':<6}{'CTA':>7}{'p1':>7}{'p2':>7}{'p3':>7}{'ptop3':>8}  [中立ptop3]")
    sum_p1 = 0.0
    for h, r_a, r_n in zip(horses, result_actual, result_neutral):
        sum_p1 += r_a["p1"]
        print(f"{h['horse_name']:<14}{h['style']:<6}{h['cta_z']:>7.2f}"
              f"{r_a['p1']:>7.3f}{r_a['p2']:>7.3f}{r_a['p3']:>7.3f}{r_a['ptop3']:>8.3f}"
              f"  [{r_n['ptop3']:.3f}]")

    print(f"\np1合計(当日実況シナリオ): {sum_p1:.4f} (1.0付近が妥当)")
    all_in_range = all(0 <= r["p1"] <= 1 and 0 <= r["p2"] <= 1 and 0 <= r["p3"] <= 1
                        for r in result_actual)
    print(f"全馬 p1/p2/p3 が[0,1]範囲内: {all_in_range}")

    conn.close()


def _self_test_from_db():
    """this_week_races.jsonが無い/使えない場合、DBの直近レースで代替テスト。"""
    import time
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT race_id, date, venue, race_num, surface, distance
        FROM race_laps ORDER BY date DESC LIMIT 1
    """).fetchone()
    race_id, date, venue, race_num, srf, dist = row
    runners = conn.execute("""
        SELECT horse_name, umaban, jockey FROM results
        WHERE race_id=? ORDER BY umaban
    """, (race_id,)).fetchall()
    horses = [{"horse_name": hn, "umaban": uma or (i + 1), "jockey": jk or "",
               "gate": min(8, ((uma or i + 1) - 1) // 2 + 1), "style": None}
              for i, (hn, uma, jk) in enumerate(runners) if hn]

    class_par = build_class_par_table(conn, verbose=False)
    k_cls = calibrate_k_cls(conn, verbose=False)
    bias_map = load_same_day_bias_map(conn)
    pace_baseline = build_baseline_table(conn, verbose=False)

    race_info = {"venue": venue, "distance": dist, "track_cond": "良",
                 "num_horses": len(horses), "date": date, "surface": srf, "race_id": race_id}
    print(f"=== DB代替テスト: {race_id} {srf}{dist}m ({len(horses)}頭) ===")
    horses = precompute_horse_features(conn, horses, race_info, class_par, k_cls, bias_map,
                                        pace_baseline)
    t0 = time.time()
    result = run_mc123(horses, race_info, n_mc=N_MC_DEFAULT, wind={"tail_home": 1.0, "gust_max": 5.0})
    t_mc = time.time() - t0
    print(f"MC実行時間: {t_mc:.2f}秒")
    sum_p1 = sum(r["p1"] for r in result)
    for h, r in zip(horses, result):
        print(f"  {h['horse_name']:<14}{h['style']:<6} p1={r['p1']:.3f} p2={r['p2']:.3f} "
              f"p3={r['p3']:.3f} ptop3={r['ptop3']:.3f}")
    print(f"p1合計: {sum_p1:.4f}")
    conn.close()


if __name__ == "__main__":
    _self_test()
