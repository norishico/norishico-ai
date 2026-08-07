"""
mc_dyn_engine.py — 距離特化MC(案C)Phase 1: 生理モデル単体。

状態: v_base(巡航速度)・v_kick(末脚上乗せ)・e_max(エネルギー予算)。
戦術(位置取り競合)・コーナー・混雑・レーンはPhase 2-3で追加。ここでは「レール上を
1頭だけ走らせる」最小構成で、生理モデルの基本挙動(巡航→仕掛け→減速)が実測ラップと
整合するかを検証する。

パラメータのpar自動アンカー: SPD=80(平均的能力)の馬が仕掛け区間だけキックする走行で
ちょうどpar_time(venue×surface×distance上位3頭平均)になるよう、v_base(SPD=80時)を
二分探索で解く。以降SPD/SPR/STAの残差はこのv_baseからの加減算として効く。
"""
import math
import random

A1 = 0.033        # SPD -> m/s換算(1ptあたり)
E0 = 250.0        # エネルギー予算基準(m、STA=75時)
BE = 5.0          # STA残差 -> エネルギー予算換算
K0 = 0.4          # 末脚基準(m/s、SPR=80時)【2026-08-01 Phase1較正済み値、旧初期値0.8から変更】
K1 = 0.02         # SPR残差 -> 末脚換算
PHI_FADE = 0.04   # エネルギー枯渇時の速度低下率(較正の結果、初期値のまま最良)
E_REF = 100.0     # 末脚上限の減衰基準(m)
KICK_START_M = 600.0  # 残りこの距離から仕掛け開始
ACCEL_FRAC = 0.96     # 発走直後(最初の区間)の速度掛け目【2026-08-01 Phase1較正済み値、旧初期値0.90から変更】
ACCEL_ZONE_M = 200.0  # 加速区間の長さ(m)

# --- 馬場状態による巡航速度補正(2026-08-01追加) -----------------------------
# 【発見】keiba.db実測(全venue×distance横断、良基準の相対勝ちタイム比・サンプル数
# 加重平均、芝70グループ/ダート36グループ)で、芝は馬場が悪化するほど遅くなる
# (稍+1.09%/重+1.86%/不良+3.69%)一方、ダートは逆に速くなる(稍-0.33%/重-1.39%/
# 不良-1.47%、雨で締まって高速化する実際の傾向と一致)ことを確認。kappa_press/
# D_SCALEと同様、芝とダートで符号が逆になるため表面別辞書とする。値は速度倍率
# (=1/タイム比)。この機能はデフォルト(track_cond_factor=1.0)では無効なので、
# 既存のPhase1/Phase2ゲート検証には一切影響しない(オプトインの新機能)。
TRACK_COND_V_FACTOR = {
    "芝": {"良": 1.0, "稍": 1 / 1.0109, "重": 1 / 1.0186, "不": 1 / 1.0369, "不良": 1 / 1.0369},
    "ダ": {"良": 1.0, "稍": 1 / 0.9967, "重": 1 / 0.9861, "不": 1 / 0.9853, "不良": 1 / 0.9853},
}

# --- 坂(最終直線の勾配)による速度補正(2026-08-01追加) -----------------------
# venue_elevation.md(2026-07-20調査)で定量化できた5場(東京・中山・中京・阪神・小倉)の
# 最終直線の坂を反映する。坂ゾーンはDB(course_slope)から呼び出し側(calibrate_mc_dyn_
# phase2.py)が取得しbuild_slope_zones()で絶対距離に変換、_run()/simulate_field()には
# 素の(start, end, grade)ゾーンのみを渡す(mc_dyn_engine.pyはDB非依存を維持)。
# MIN_V(速度下限)はPhase2セクションで定義済みのものを流用(モジュール読み込み後の
# グローバル名解決のため、定義順は_run()の呼び出しタイミングに影響しない)。
# 【2026-08-01較正結果】k_slopeを0(無効)〜45で振ったところ、ゲート1相関が0.829(k_slope=0)
# から単調に悪化し45で0.779まで低下した(ゲート2/3への影響は軽微)。これは坂が無意味
# なのではなく、kappa_press_turf/d_scale_turf等の表面別戦術パラメータが実データ
# (=既に現実の坂の影響を含んだ実測ラップ)を較正する過程で坂由来のペース傾向を
# 暗黙のうちに吸収済みだったため、上から明示的に坂を追加すると二重計上になり
# 悪化する。ゲート指標を優先しk_slope=0(実質無効)を採用。course_slopeテーブル・
# build_slope_zones()等のインフラ自体は稼働状態のまま温存し、将来レース単位の
# 個別予測など別用途で再度有効化できるようにしておく。
K_SLOPE = 0.0     # 勾配(rise/run)1あたりの速度減算量(m/s)。座標降下法で調整対象、上記の理由で0採用(芝)
# 【2026-08-07追加】ダート専用のk_slope。芝のk_slope=0採用(上記コメント: 表面別戦術
# パラメータが最終直線の坂を暗黙吸収済みで二重計上)とは前提が異なる: ダート短距離の
# セル間実測H率差(77.7-99.3%)の調査(2026-08-07)で「道中(向正面〜3-4角)の勾配」が
# 主要因候補と判明し、course_slopeにダート道中ゾーン(dirt-midrace-20260807タグ、
# 京都=3角の上り/東京=向正面の上り/中山=道中の下り、JRA公式の高低差・位置記載由来)を
# 新規収録した。これらは従来較正が一度も見ていない情報のため二重計上にならない。
# 【2026-08-07較正結果: 不採用=0.0維持】1次元スイープ{0,5,10,15,20,30}(訓練=東京/中山/
# 京都/小倉/福島のダート15セル、検証=函館/札幌/阪神/中京9セル、n_sim=300、
# scratchpad/kslope_dirt_sweep.py)で、mean|simH-realH|は訓練15.17→15.10pt(ks=15、
# ノイズ水準)/検証11.47-11.75ptと実質フラット、ks=30では検証悪化。狙いの京都ダ1200は
# 100→96.3%(ks=30)と方向は正しいが必要量(実測77.7%まで-22pt)の2割未満しか動かず、
# 逆に京都ダ1400(86.7→82.0%、実測92.5%)・東京ダ1300(93.3→96.0%、実測53.6%)・
# 中山ダ1800(67.3→70.0%、実測35.9%)は誤方向に動いた。ゴール基準の同一坂ゾーンが
# レース距離によって前半/後半のどちらに当たるかが変わり符号が割れること、上り→下りの
# 対で前後半バランスへの寄与がほぼ相殺されることが原因。「道中の坂の有無と実測H率の
# 相関」(2026-08-07調査)自体は実在するが、単純な速度加減算では再現できない。
# course_slopeのdirt-midrace-20260807行・表面別配線・build_slope_zonesの部分クリップは
# k_slope_dirt=0で完全no-op(公式ゲートでビット単位一致確認済み)のため、データ資産と
# して温存する。gate2(新潟=坂ゾーンなし)は全スイープ点で不変(99.7%)を確認済み。
K_SLOPE_DIRT = 0.0  # ダート向け(2026-08-07スイープの結果、不採用=0.0を維持)


def segment_lengths(n, distance):
    """区間(lap)の実際の長さ(m)。distance%200!=0の場合、先頭区間だけ端数になる
    (fetch_laps.py:2026-08-01修正と同一の規則)。"""
    remainder = distance % 200 if distance else 0
    if remainder == 0:
        return [200.0] * n
    return [float(remainder)] + [200.0] * (n - 1)


def build_slope_zones(distance, slope_defs):
    """slope_defs: [(remaining_start_m, remaining_end_m, grade), ...]
    (remaining_start_m > remaining_end_m、ゴールからの残り距離ベース、DBのcourse_slope
    由来)。レース距離distanceが坂の入口(remaining_start_m)に届かない短距離戦では、
    そのゾーンを除外する。戻り値: [{"start", "end", "grade"}, ...] (発走からの絶対距離)。"""
    zones = []
    for remaining_start_m, remaining_end_m, grade in slope_defs:
        # 【2026-08-07変更】従来はremaining_start_mがレース距離を超えるゾーンを丸ごと
        # 除外していたが、道中勾配ゾーン(ダート、dirt-midrace-20260807)の追加により
        # 「発走ラインがゾーンの途中にある」ケース(例: 東京ダ1300と残り1350-1050mの
        # 向正面の上り)が生じるため、部分クリップに変更。ゾーン全体が発走より手前
        # (distance <= remaining_end_m)の場合のみ除外。既存データ(最終直線の坂、
        # remaining_start<=520m)は全レース距離で従来と同一動作。
        if distance <= remaining_end_m:
            continue
        zones.append({"start": max(0.0, distance - remaining_start_m),
                       "end": distance - remaining_end_m, "grade": grade})
    return zones


def find_slope_grade(zones, pos):
    """posが含まれる坂ゾーンの勾配(rise/run、正=上り)を返す(無ければ0.0)。"""
    if not zones:
        return 0.0
    for z in zones:
        if z["start"] <= pos < z["end"]:
            return z["grade"]
    return 0.0


def _run(seg_lens, distance, v_base, spd_res=0.0, spr_res=0.0, sta_res=0.0,
         accel_frac=ACCEL_FRAC, accel_zone_m=ACCEL_ZONE_M, k0=K0, phi_fade=PHI_FADE,
         slope_zones=None, k_slope=K_SLOPE, track_cond_factor=1.0):
    """レール上1頭走行シミュレーション。戻り値: (総タイム, 区間ラップ秒のリスト)。
    発走直後(accel_zone_m以内)はスタンディングスタートの加速により速度を割り引く。
    track_cond_factor: 馬場状態による巡航速度倍率(2026-08-01追加、既定1.0=無補正で
    既存ゲートへの影響なし)。表面別のTRACK_COND_V_FACTORから呼び出し側が解決して渡す。"""
    e_max = E0 + BE * sta_res
    E = e_max
    cum = 0.0
    laps = []
    v_flat = (v_base + spd_res * A1) * track_cond_factor
    for seg_len in seg_lens:
        remaining_to_finish = distance - cum
        seg_mid = cum + seg_len / 2
        if remaining_to_finish <= KICK_START_M:
            v = v_flat + (k0 + K1 * spr_res) * min(1.0, E / E_REF)
            if E <= 0:
                v = min(v, v_flat * (1 - phi_fade))
        elif seg_mid <= accel_zone_m:
            v = v_flat * accel_frac
        else:
            v = v_flat
        grade = find_slope_grade(slope_zones, seg_mid)
        if grade:
            v = max(MIN_V, v - k_slope * grade)
        if v > v_flat:
            E -= (v - v_flat) * seg_len
        t = seg_len / v
        laps.append(t)
        cum += seg_len
    return sum(laps), laps


def anchor_v_base(distance, par_time_sec, n_segments=None, **kw):
    """SPD=80(残差0)の馬がpar_time_secちょうどになるv_baseを二分探索で解く。"""
    n = n_segments or max(3, round(distance / 200))
    seg_lens = segment_lengths(n, distance)
    lo, hi = 10.0, 25.0
    for _ in range(50):
        mid = (lo + hi) / 2
        t, _ = _run(seg_lens, distance, mid, **kw)
        if t > par_time_sec:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2, seg_lens


def simulate_solo(distance, par_time_sec, spd=80.0, spr=80.0, sta=75.0, **kw):
    """par自動アンカー込みで1頭シミュレーション。**kwでaccel_frac/k0/phi_fade等を上書き可能。
    戻り値: dict(total_time, laps, seg_lens, v_base)。"""
    v_base, seg_lens = anchor_v_base(distance, par_time_sec, **kw)
    spd_res, spr_res, sta_res = spd - 80.0, spr - 80.0, sta - 75.0
    total_time, laps = _run(seg_lens, distance, v_base, spd_res, spr_res, sta_res, **kw)
    return {"total_time": total_time, "laps": laps, "seg_lens": seg_lens, "v_base": v_base}


# ============================================================================
# Phase 2 — 戦術コントローラ(P0-P4フェーズ制)+複数馬シミュレーション
# ============================================================================
"""
Phase1は「レール上を1頭だけ走らせる」生理モデルだったが、Phase2ではフィールド
(複数頭)を同時にシミュレーションし、位置取り競合・コーナー・混雑(壁/追い越し)を
追加する。Phase1で較正済みの生理パラメータ(accel_frac/k0/phi_fade/A1/E0/BE/K1/E_REF/
KICK_START_M)は一切変更しない。ただし本セクションのP0(スタートダッシュ)は
Phase1のaccel_frac方式(発走直後を一律accel_frac倍で減速)を置き換える新しい
脚質別モデルであり、simulate_field()はaccel_frac/ACCEL_ZONE_Mを使わない
(Phase1のsimulate_solo/_runは無変更のままこれらを使い続けるので後方互換は保たれる)。

このセクションはDBに一切アクセスしない(pure computation)。course_layout/
course_start_layoutからのデータ取得・q_star実測値の集計等、DBへのSELECTは
呼び出し側(calibrate_mc_dyn_phase2.py)の責務とする(Phase1のcalibrate_mc_dyn_
phase1.pyがDBクエリを担当し、mc_dyn_engine.pyは疎結合を保つ、という既存の
役割分担を踏襲)。
"""

# --- 脚質別固定パラメータ(探索しない) ---------------------------------------
D_STYLE = {"逃げ": 1.6, "先行": 1.1, "差し": 0.6, "追い込み": 0.2}  # スタートダッシュ力(m/s、フル競争時)
D_SCALE = 1.0            # D_STYLEへの一律スケール(仕様書に具体値の指定なし、1.0を既定値とする。ダート向け)
# 【2026-08-01追加】kappa_press/dash_min_fracを芝ダート分離・ゲート1=0.792達成後も、
# 芝はkappa_press=0/dash_min_frac=0まで下げても+9.15ptのHペース過大評価が残る"床"が
# あった。切り分けたところD_STYLE(脚質別ダッシュ力)を実質ゼロ化すると符号が逆転
# (-6.96%)まで動く一方、a_lat(コーナー速度上限)を絞る方向やrho_saveの強化は
# ほぼ効果がなかった(前者はむしろ悪化)。D_STYLEが主因と判明したため、芝だけ
# D_SCALEを部分的に弱めることで対応(完全ゼロ化は行き過ぎ、相関自体も悪化するため
# 不採用)。
D_SCALE_TURF = 0.85      # 芝向けD_STYLEスケール(座標降下法で調整対象、2026-08-01較正済み値。
                          # ゲート1相関0.792->0.813に改善、ゲート2/3への悪影響なしを確認済み)

# --- ダッシュの競争依存スケール(2026-08-01追加) ------------------------------
# 【発見】Phase2初版はD_STYLEを無条件のフルダッシュとして使っており、「単騎で先頭に
# 立てるなら無理に飛ばさない」という自重が表現できず、芝中長距離レース(競争相手が
# 実質いない単騎逃げが多い)でHペースを過大予測していた(実測3-10%に対しシミュ24-65%)。
# 同格以上に積極的な脚質のライバル数に応じてダッシュ強度をスケールし、単騎なら弱め、
# ライバルが増えるほどフルダッシュに近づける。
STYLE_AGGRESSION = {"逃げ": 0, "先行": 1, "差し": 2, "追い込み": 3}  # 小さいほど前へ行きたがる
DASH_MIN_FRAC = 0.85      # ライバル0(単騎)時のダッシュ割合(座標降下法で調整対象、2026-08-01較正済み値)
DASH_RIVAL_SAT = 2.0      # このライバル数でダッシュ割合が1.0(フル)に到達

# --- 引込線(ポケット)発走のダッシュ底上げ(2026-08-01追加) -------------------
# 【発見】新潟ダ1200等の「引込線発走→極端なHペース」は、脚質のライバル数とは無関係に
# 発走地点そのものの構造(全馬が横一列でフェアに並ぶため枠順優位が消え、先行争いが
# 一律に激化する)が原因(Phase0a follow-up調査で確認済み)。DASH_MIN_FRACによる
# ライバル数依存のスケールだけでは、たまたま逃げ馬のライバルが少ない引込線発走レース
# (新潟ダ1200等)のダッシュも一緒に弱めてしまい、H率を過小評価する不具合が発生した
# (95.0%->78.3%に悪化)。引込線発走の場合はダッシュ割合の下限を底上げすることで、
# 「ライバル数に関わらず発走地点そのものが先行争いを激化させる」効果を独立に表現する。
CHUTE_DASH_FRAC = 0.9     # 引込線発走時のダッシュ割合の下限(2026-08-01較正済み値。
                          # ダート限定適用、calibrate_mc_dyn_phase2.pyのapply_chute_boost参照)
# 【2026-08-07監査H1対応: 本機構は現較正下で意図的に「実質無効」のまま温存する】
# dash_frac = max(dash_frac, chute_dash_frac) は、dash_min_frac=0.85/dash_rival_sat=2.0の
# 現較正では効き代がほぼ無い(争いのある馬は0.925-1.0、単騎逃げのみ0.85→0.9の+0.05)。
# 機構導入時(dash_min_frac探索域0.15-0.55)には大きな効果があったが、dash_min_fracの
# 再較正(0.85)でベースのダッシュ自体が引込線相当の強度になり、役割を吸収された。
# 【再確認 2026-08-07、追い越しペナルティ修正+σ/solo_ease/先導権決着の全再較正後】
# top60のchuteダート9セルで chute_dash_frac {0.0(OFF相当), 0.6, 0.9, 1.0} をrun_cell比較
# (n=300、scratchpad/chute_deadcode_check.py): 0.0と0.6は全セル完全同一、0.9はOFF比
# 最大±1.0pt(MCノイズ内)、最大ブースト1.0でも≤2.3pt。一方、chuteセルの実測H率は
# 機構OFFで既に十分再現できている(京都ダ1200を除く8セル平均|誤差|≈2.7pt。gate2の
# 新潟ダ1200も実測98.2% vs OFF99.7%)。誤差の符号は正負混在でブースト復活は改善に
# ならないため、値の変更・削除とも行わず「無効だが温存」を明示的な設計判断とする
# (dash_min_fracが将来0.9未満に再較正されれば自動的に意味を持つ安全弁)。
# 注: 京都ダ1200のシミュH過大(実測77.7% vs シミュ99.7%、+22pt)はcdf全値で不変の
# chute無関係な別問題として残課題に記録。

# --- 戦術パラメータ(座標降下法で調整対象。2026-08-01較正済み値に更新
# 【教訓】Phase1と同じ不具合を再発: calibrate_mc_dyn_phase2.py --calibrateが較正結果を
# JSONに保存するだけでこのモジュール既定値に反映していなかったため、再実行のたびに
# 座標降下法が毎回この初期値からやり直しになっていた(前回較正で見つけた良い領域を
# 引き継げない)。以後、較正するたびにここを更新すること】 ---
KAPPA_PRESS = 0.3         # 位置取り競合の圧力(m/s、表面分離前の後方互換用フォールバック値)
# 【2026-08-01追加】kappa_pressは表面(芝/ダート)で必要な値が逆方向だったため分離。
# 実測診断: プールした単一グローバル値では、ダートはHペース側へのバイアスが不足し
# (kappa_press=0.3でH率-10.9pt過小評価)、芝は逆にH側へ過大評価(同+15.5pt)する
# 綱引きが発生していた。calibrate_mc_dyn_phase2.py側でcell["surface"]により
# KAPPA_PRESS_DIRT/KAPPA_PRESS_TURFのどちらを使うか選択する(このモジュールは
# DB非依存のためsurface判定自体は呼び出し側=calibrate_mc_dyn_phase2.pyの責務)。
KAPPA_PRESS_DIRT = 1.2    # ダート向け(座標降下法で調整対象、2026-08-01較正済み値)
KAPPA_PRESS_TURF = 0.0    # 芝向け(座標降下法で調整対象、2026-08-01較正済み値。D_SCALE_TURF
                          # 追加後の再較正でkappa_press単体の寄与はゼロに収束)
K_GAP = 0.05              # 車間維持のゲイン(/s)
RHO_SAVE = 1.3            # 先頭馬が脚を溜める減速量(m/s、2026-08-01較正済み値)
A_LAT = 2.1               # コーナー横加速度上限(m/s^2、2026-08-01較正済み値)

# --- P0スタートダッシュの作用距離キャップ(2026-08-04パラメータ化) -----------------
# 従来はsimulate_field内に400.0がハードコードされていた(dash_end=min(d_c1,400.0))。
# 【発見】単騎逃げイージング(comp_ease)が東京だけ効かない(芝1800単騎S率5.8% vs 実測
# 62.9%)原因を反実仮想実験で切り分けたところ、会場固有の問題ではなく「d_c1が長い
# コースはダッシュ窓がこのキャップ(400m)まで張り付き、前半が構造的に速くなりすぎる」
# という全会場共通の仮定の問題だった。証拠(2026-08-04、n=500/条件):
#   東京芝1800: 窓400m→S=5.8% / 300m→21.2% / 275m→34.2% / 200m→81.4%(実測62.9%)
#   対称チェック 中京芝2000(実窓274.5m、S=74.2%正常)を窓400mに伸ばすと13.8%に崩壊
#   東京芝2000: 窓400m→S=16.2% / 275m→77.2%(実測72.6%)
# 実際の発馬ダッシュは200-300m程度で落ち着き、コーナーが遠いからといって400mまで
# 全開が続くわけではない、という物理的解釈とも整合する。
DASH_CAP_M = 400.0        # P0ダッシュが働く距離の上限(m)。座標降下法で調整対象
# 【2026-08-04採用: 距離テーパー】ただし一律のキャップ短縮はトレードオフになることも
# 同日のフルゲート実験で確認済み: cap=300一律はゲート1相関0.885→0.813に悪化(犠牲は
# 芝スプリント: 札幌/小倉/函館/福島芝1200の実測H60-79%がsim20%台に崩壊)、cap=250は
# ゲート2も崩す(88.3%)。つまりダッシュ窓は(1)発馬ダッシュと(2)短距離戦の持続的な
# 速い前半、の二役を担っており、識別変数は会場ではなく距離。芝の1400m以下は400mが
# 必要で、1400m超は300mが適正(ダートは④診断でH経済が別問題と判明しており400のまま)。
# 距離2段階テーパー(値はスイープ済み{400,300}のみ、閾値1400はセル別デルタ診断で
# 悪化セルが全て<=1400・改善セルが>=1800だったことに基づく)のフルゲート:
# ゲート1=0.888(一律400の0.885からさらに改善)/ゲート2=91.7%不変/ゲート3全OK
# (3a+41.0pt/3b+2.2pt)。東京芝1600のS率1.7%→41.7%(実測39.1%)等、d_c1が長い東京の
# セルの詰まりも大幅改善。注意: 閾値・値の選択は同じ60セルデータに基づく(真のOOSは
# 将来年度データ待ち)。
DASH_CAP_TURF_LONG_M = 300.0   # 芝・distance>1400m向けダッシュ窓キャップ
DASH_CAP_TAPER_DIST = 1400     # この距離(m)以下は従来のDASH_CAP_M(400)を使う


def dash_cap_for(surface, distance):
    """表面×距離からP0ダッシュ窓キャップを返す(採用済みの距離2段階テーパー)。
    呼び出し側(calibrate_mc_dyn_phase2.run_cell / predict_race_pace / predict_race_formation)
    がsimulate_field(dash_cap_m=...)に渡す。simulate_field自体の既定値はDASH_CAP_M=400
    (レガシー互換)のまま変えない。"""
    if surface == "芝" and distance and distance > DASH_CAP_TAPER_DIST:
        return DASH_CAP_TURF_LONG_M
    return DASH_CAP_M


# --- 芝ダッシュ窓のd_c1非依存化(2026-08-05追加) --------------------------------
# 【背景】会場バイアス調査で、芝ルートセルのシミュS率のセル間変動がほぼ
# dash_end=min(d_c1, cap)のd_c1成分だけで決まっており(窓±100mでS率40-70pt動く)、
# それが実測の駆動因子(クラス・頭数・コース規模)と無関係な軸=JRAコース在庫のd_c1の
# 偶然の分布(ローカル=短い/東京=長い)でセルごとのS率を歪めていたことが判明
# (セル横断でシミュS率と実測S率がcorr=-0.18と逆向き)。発馬ダッシュの長さは
# 「最初のコーナーの位置」ではなく発走そのものの性質(距離テーパー済みcap)で決まる、
# という一般則に変更する(会場名不使用)。ダートはkappa_press(押し合い)の作用窓が
# d_c1に結び付いており独立の較正体系のため従来のmin(d_c1, cap)を維持。
# 検証: 窓のみの変更はS絶対水準を下げすぎるため(gate3b +2.2→-9.6pt)、レース属性
# ペースバイアス(pace_bias)+イージング再較正(訓練セルのみ)とセットで採用する。
def dash_window_for(surface, distance):
    """simulate_fieldのdash_window_mに渡す値。芝=cap固定(d_c1非依存)、ダート=None(レガシー)。"""
    if surface == "芝":
        return dash_cap_for(surface, distance)
    return None
# 【発見】kick_trigger修正(コーナー入口→出口)後も東京だけ誤差が残存(1800m/2000m等、
# 会場を問わずkick_trigger位置が同一になる距離帯で顕著)。原因はd_c1(発走〜第1コーナー)
# そのものの長さ: 東京は1800mでd_c1=719.9m(レースの40%)に対し小倉は172.8m(9.6%)と、
# 「押し合い」が働き続ける距離が会場によって大きく異なり、P0ダッシュ(dash_end=
# min(d_c1,400.0)で既に距離キャップ済み)と違い、P1位置取り競合(kappa_press)には
# d_c1以外の上限が無かった。dash_endと同じ発想でPRESS_CAP_Mを導入する。
PRESS_CAP_M = 400.0       # 位置取り競合が働く距離の上限(m)。座標降下法で調整対象

# --- モデル化のための設計判断(仕様書に数値指定が無いため明記する既定値) -------
TARGET_GAP_M = 3.0        # P2追走時の目標車間(m、おおよそ1馬身)

# --- 構成依存イージング(単騎逃げの余裕、2026-08-04追加) ----------------------
# 【発見】従来の脚溜め条件「後続との時間差gap_s >= follower_ease_s(0.5秒≈8m)」は、
# 追走馬のP2コントローラ(target_gap_m=3.0m≈0.18秒に収束)と構造的に矛盾しており、
# 実質一度も発動しない死にコードだった(実測: 中京芝2000単騎逃げでrho_saveを0→3.0に
# 振ってもS率4.5%→5.5%と無反応。ゲートを撤廃(follower_ease_s=0.0)すると同条件で
# S率89.8%まで跳ね、経路自体は機能することを確認)。その結果、実データに存在する
# 「逃げ馬頭数とスローペース率の強い逆相関」(実測: 芝プールで単騎S率47.1% vs
# 複数逃げS率20.0%、中京芝2000では単騎77.2%/2頭57.4%/3頭以上39.0%)をほぼ再現
# できていなかった(シミュはどの頭数でもS率2-6%)。
# 対策: gap基準とは独立に「レース構成(逃げ馬頭数)に応じた恒常的な脚溜め」を追加する。
# 先頭馬はP2巡航中、逃げ馬のライバルが少ないほど強く緩める:
#   comp_ease = rho_save * solo_ease_scale * max(0, 1 - (n_nige - 1) / ease_rival_sat)
# solo_ease_scale=0.0(既定)で完全に無効となり、従来動作とビット単位で一致する
# (浮動小数点演算・乱数消費とも不変)。表面別の較正値はcalibrate_mc_dyn_phase2.py側で
# solo_ease_scale_turf / solo_ease_scale_dirt として選択して渡す(kappa_pressと同じ分離方式)。
SOLO_EASE_SCALE = 0.0     # simulate_field()の既定値=無効(レガシー互換)。呼び出し側が表面別較正値を渡す
EASE_RIVAL_SAT = 16.0     # 逃げ馬ライバルがこの頭数で構成依存イージングが完全消滅
# 【2026-08-07再較正: scale 0.7→0.6 / sat 12→16】追い越しペナルティバグ修正(08-06)+
# pace_noise_sigma再較正(芝0.7→0.9)後の環境で、旧値(バグあり環境で較正)を再較正。
# 方式は2026-08-04/05と同一: 訓練会場(東京/中山/京都/小倉/福島)の芝≥1600・非chuteセルの
# 単騎/複数バケット実測S率再現(train 22セルバケット)でグリッド{scale 0.4-0.85}×
# {sat 4-24}を選定、検証会場(函館/札幌/阪神/中京、12セルバケット)は選定に不使用。
# 結果: (0.6, 16)がtrain|e|最小(14.1→13.6pt)かつ検証セルでも最良(11.0→8.6pt)で汎化確認。
# 単騎バイアスはscaleのみで決まり(satは単騎に無関係)、0.6で+1.4pt(train)/+1.8pt(valid)と
# ほぼ無バイアス。sat=24は反転悪化し境界問題なし。複数逃げS率は依然-8pt前後の過小が残る
# (scale/satでは埋まらない構造的不足、既知の残課題)。手順: scratchpad/soloease_recal_grid.py
# 【2026-08-05更新: 4.0→12.0】芝dash窓のd_c1非依存化(dash_window_for)+レース属性
# ペースバイアス(pace_bias)の採用に伴い、訓練セル(東京/中山/京都/小倉/福島の芝
# ≥1600、検証会場は不使用)のみのグリッドサーチ{scale 0.6-0.9}×{sat 4/6/8/12}で
# 再較正した値(旧4.0は旧dash窓体系とセットの較正値で、窓固定後は複数逃げのS率が
# 過小になるため減衰を緩めた)。検証会場(函館/札幌/阪神/中京)での事後確認:
# 全|bias| 14.2→12.2pt/単騎15.6→11.1pt/複数13.4→12.8ptと全て改善し汎化を確認。
# ダートはsolo_ease_scale_dirt=0.0のためこの値の影響なし。
# 【2026-08-04較正結果】表面別グリッドサーチ(芝4セル×逃げ頭数3バケット、実測構成
# サンプリング、n_sim=250/バケット)で芝はscale=0.7/sat=4が最良。ダートはscale=0〜0.3の
# いずれでもシミュS率がほぼ動かず(実測の単騎S率8.1%を再現できない)、scale=0.3は
# 単騎逃げのH率をわずかに悪化させるため0.0(レガシー)を維持。フルゲートへの影響:
# gate1相関0.879→0.885(改善)、gate2新潟H率91.7%(不変)、gate3単騎S率4.7%→33.5%/
# 複数S率3.5%→9.6%(実測プール28.1%/8.6%にほぼ整合)、会場別方向性(単騎>複数)は
# 芝10会場全てOK。
SOLO_EASE_SCALE_TURF = 0.6   # 芝向け較正済み値(calibrate_mc_dyn_phase2.py/predict_race_pace.pyが使用。
                              # 2026-08-07再較正で0.7→0.6、EASE_RIVAL_SATのコメント参照)
SOLO_EASE_SCALE_DIRT = 0.0   # ダート向け(構成依存イージングでは実測S率を再現できず、レガシー維持)

# --- 複数逃げの先導権決着(レースレベルの戦術的裁量、2026-08-07追加) --------------
# 【背景】gate3a超過(+29.2pt)の原因分析で「複数逃げレースのS率がsolo_ease系の全較正点で
# 約8pt過小(=実際よりハイペース寄り)」という構造的不足を確認。芝はkappa_press_turf=0の
# ためP1の逃げ牽制ブースト(len(nige_idxs)>=2分岐)は芝では死んでおり、過熱の実体は
# (1)P0ダッシュ: 逃げ分類馬が増えるほどD_STYLE=1.6のフルダッシュ馬が増え、leader_laps
# (各マーカー最速到達のmax統計)の前半が確実に速くなる (2)単騎なら発動し得るgap_ease
# (逃げ馬が後続を0.5秒以上離した時のrho_saveフル脚溜め)が、2頭目の逃げ馬が直後にいる
# ため封殺される、の2経路。つまり「過去走ベースで逃げに分類された馬は全レースで必ず
# 全力で先頭を主張する」という暗黙の仮定が実戦(騎手判断で一方が譲る・控えるのが常態)
# より強すぎた。
# 【機構】レース開始時に1回だけ「先導権が序盤で決着するか」を抽選し、決着した場合は
# 逃げ馬のうちランダムに選んだ1頭を残して、他の逃げ馬を戦術的に「先行」として扱う
# (P0ダッシュ強度・ライバル数contest・comp_easeのn_nige・P1の逃げ判定・q_star目標の全て。
# 出力のstylesは入力の脚質のまま保持)。当該レースの実結果(pos4等)は一切使わない
# (入力の過去走ベース脚質+レースレベル乱数のみで構成。循環参照なし)。
# nige_settle_prob=0.0(既定)では抽選せず乱数を消費しない=レガシーとビット単位一致。
# 単騎レース(逃げ0-1頭)でも消費しないため、単騎バケットの挙動はprobに完全不変。
NIGE_SETTLE_PROB = 0.0       # simulate_field()の既定値=無効(レガシー互換)
# 【2026-08-07較正】1次元スイープ{0,0.15,0.3,0.45,0.6,1.0}(訓練会場の芝≥1600・非chuteの
# 単騎/複数バケット実測S率再現、scale=0.6/sat=16固定、n_sim=200)でp=0.6がtrain|e|最小
# (13.6→13.4pt)。複数バケットのバイアスは訓練-7.9→-2.7pt、検証会場でも-4.5→+0.2ptと
# 符号まで解消し汎化確認。単騎バケットは全p点で不変(+1.4pt、設計どおり)。
# 手順: scratchpad/nige_settle_sweep.py
NIGE_SETTLE_PROB_TURF = 0.6  # 芝向け較正済み値(calibrate/predict系が使用)
NIGE_SETTLE_PROB_DIRT = 0.0  # ダートは対象外(実測の単騎/複数S率差自体が小さくS生成機構未実装のため)
# --- レースレベルのペース意図ノイズ(2026-08-05追加、既定0.0=無効) --------------
# 【発見】実測の前後半バランス(front_avg-back_avg)のレース間stdは会場×距離を問わず
# ほぼ一様に0.31-0.46あるのに対し、シミュは0.215-0.347しかなく、特にダート中距離・
# 芝ルートで大幅に不足する(阪神ダ1800単騎: 実測0.419 vs シミュ0.217)。実データ検証で
# この不足分は「実力差(オッズ)・騎手傾向・先行頭数・馬場・クラス・日単位の共通要因
# (ICC=0.026)・風(r=-0.02)のいずれでも説明できない」(多変量OLSでもR²≈0.05-0.13)
# レース固有の変動と判明した。つまり「同じ構成でも日によって速い/遅い両方が起きる」
# のは観測可能な事前情報では予測できない先頭馬のペース裁量そのもの。
# 対策: 1レースにつき1回抽選するゼロ平均ノイズを、P2巡航フェーズの先頭馬のみに加算する。
# 巡航が長いレース(中距離)ほど自動的に効きが大きく、巡航がほぼ無い短距離Hアンカー
# (新潟ダ1200等、実測stdも最小)には自動的にほぼ効かない — セル別調整が不要な一般機構。
# pace_noise_sigma=0.0(既定)では乱数を消費せず、従来動作とビット単位で一致する。
PACE_NOISE_SIGMA = 0.0    # simulate_field()の既定値=無効(レガシー互換)。呼び出し側が表面別較正値を渡す
# 【2026-08-05較正結果】表面別の必要量を実測balance stdの再現で決定(スイープ値
# {0.0,0.3,0.5,0.7,0.9}、B4計測: 芝は0.7で実測とほぼ一致(函館芝1800: sim0.375 vs
# 実測0.374、中京芝2000: 0.424 vs 0.404)、0.9では過剰(東京芝1600: 0.496 vs 0.391)。
# ダートは0.9で一致(阪神ダ1800: 0.410 vs 0.404)、0.7では不足(0.330))。
# フルゲートへの影響: gate1相関0.888→0.904(改善)、gate2新潟ダ1200 H率91.7%(不変)、
# gate3a +41.0→+25.9pt(実測リファレンス19.4-27.3ptの帯内に接近)、gate3b +2.2→+2.6pt、
# gate3c ダ単騎S 0.3%→10.7% vs 複数5.3%(差+5.4pt、実測差5.8ptとほぼ整合し、機構実装後の
# 引き上げ基準3ptもクリア)。ダート60セル平均S率バイアス-4.4pt→+0.1ptに解消。
# 【2026-08-06再較正: 芝0.7→0.9】追い越しペナルティの毎ステップ課金バグ(同日修正、
# simulate_fieldパス2/パス4参照)がs["t"]汚染経由でbalance分散を水増ししており、旧値0.7は
# その雑音込みで較正されていたことが判明(バグ修正後、芝の実測balance stdを2-3割下回った)。
# 訓練会場(東京/中山/京都/小倉/福島)のsigma反応セル16件でスイープ{0.7,0.9,1.0,1.1,1.3}し、
# 0.9と1.0が同率最良(mean|simStd-realStd|=0.050)、検証会場(函館/札幌/阪神/中京)の
# 反応セル8件では0.9が明確に優位(0.052 vs 0.062)のため0.9を採用(汎化確認済み)。
# ダートは同スイープで0.9が訓練/検証とも最良のまま(バグの分散水増しは芝に集中していた)
# のため変更なし。スプリントセル(≤1200m、巡航フェーズがほぼ無くsigma無反応)は選定から
# 除外して判断した(較正手順はscratchpad/noise_recal_sweep.py参照)。
PACE_NOISE_SIGMA_TURF = 0.9   # 芝向け較正済み値(calibrate_mc_dyn_phase2.py/predict系が使用)
PACE_NOISE_SIGMA_DIRT = 0.9   # ダート向け較正済み値(同上、2026-08-06再検証で維持)

# --- レース属性によるペース意図バイアス(2026-08-05追加、既定0.0=無効) ----------
# 【背景】会場バイアス調査(2026-08-05)で、実測の単騎/複数逃げS率のセル間変動は
# (1)クラス(単騎S率: 新馬82.0% vs 未勝利45.2%、37pt差) (2)頭数(勝上・芝1800-2200:
# ≤9頭67.4% vs 13+頭39.9%) (3)クラス×頭数×距離調整後の残差が直線長とr=+0.66、の
# 3因子で駆動されるのに対し、シミュはどれも入力に持たずdash_end(d_c1依存)だけで
# セル間変動を作っており、実測と逆向き(セル横断corr=-0.18)だったことが判明。
# 対策: レース属性(クラス群・頭数・直線長)から先頭馬の巡航速度シフト(m/s)を計算し、
# pace_noiseと同じ作用点(P2巡航の先頭馬)に加算する。会場名は一切使わない一般則。
# 【係数の出典=実データ回帰(シミュへのフィッティングではない)】訓練会場(東京/中山/
# 京都/小倉/福島)の芝≥1600レースのみで balance ~ 新馬 + 未勝利 + 頭数 + 直線長
# (距離帯・逃げ頭数バケットは交絡制御として回帰に含めるが輸出しない)を推定し、
# balance係数(秒/200m)をシミュ応答係数g(pace_bias 1m/sあたりのbalance変化、訓練
# セルで実測)で除して速度単位に変換した。検証会場(函館/札幌/阪神/中京)は係数決定に
# 一切使用していない。値はc1_fit_pace_bias.py(scratchpad)の出力を転記。
# 【2026-08-05導出値、同日v2: balance空間モデル+距離帯別g変換に再構成】
# 訓練回帰(訓練会場の芝≥1600、n=3,795、交互作用込み): 新馬+0.388(t=+17.4)/
# 未勝利-0.013(t=-0.9)/頭数-0.0365per頭(t=-9.6)/直線+0.00074per m(t=+11.6)/
# 新馬×1600 -0.125(t=-2.9)/未勝利×1600 -0.094(t=-2.7)。逃げ頭数ダミーはt≤0.5
# (既存solo_ease機構と直交=二重計上なし)。係数はbalance単位(秒/200m)で保持し、
# 距離帯別のシミュ応答係数G_BAND(pace_bias 1m/sあたりのbalance変化、D2アーキテクチャ
# 下で訓練セルのみで計測)で除して速度単位に変換する。
# 【v2の経緯】初版はグローバルg=-0.357で一律変換していたが、1600帯は応答が35%強い
# (-0.443)ため属性効果が過大に出る副作用(芝1600セルでS率+20-30pt過大)が発生。
# 距離帯別gは「フィッティングの追加自由度」ではなく変換精度の計測値である点に注意。
PACE_BAL_CLS = {"新馬": +0.388, "未勝利": -0.013}   # クラス群のbalanceオフセット(秒/200m、勝上=0基準)
PACE_BAL_CLS_1600 = {"新馬": -0.125, "未勝利": -0.094}  # 1600帯(distance<1800)の交互作用加算
PACE_BAL_K_NH = -0.0365          # 頭数1頭あたりのbalance(秒/200m)
PACE_BIAS_NH_REF = 13.3          # 頭数の基準点(訓練プール平均)
PACE_BAL_K_STRAIGHT = +0.00074   # 直線長1mあたりのbalance(秒/200m)
PACE_BIAS_STRAIGHT_REF = 402.0   # 直線長の基準点(m、訓練プール平均)
# 直線長入力のサポート範囲クリップ: 係数が推定(訓練会場292-526m)+独立検証(検証会場
# 262-474mで同符号・同水準の係数を確認)された範囲[262, 526]の外には外挿しない。
# 新潟芝(DBのcourse_layoutが外回り直線658.7mの単一variantで、内回り実走の距離にも
# 適用されてしまう)での暴走(新潟芝1400のH率が実測68.7%に対し35%まで低下)への
# 一般則としての対処(会場名は使わない)。
PACE_BIAS_STRAIGHT_MIN = 262.0
PACE_BIAS_STRAIGHT_MAX = 526.0
G_BAND = {"1600": -0.443, "1800": -0.327, "2000+": -0.392}  # 距離帯別シミュ応答係数(計測値)
PACE_BIAS_CAP = 1.2              # 安全上限(m/s)。複合極端例(新馬×少頭数×長直線等)の外挿暴走防止


def pace_cls_group(race_name):
    """race_name文字列からペースバイアス用のクラス群を返す(新馬/未勝利/勝上)。
    build_class_par.classify_class()の先頭2ルールと同一の判定(このモジュールは
    依存を持たない方針のため文字列判定のみ複製。旧表記500万下等は全て勝上側で、
    ペースバイアス上は区別不要)。"""
    rn = str(race_name or "")
    if "新馬" in rn:
        return "新馬"
    if "未勝利" in rn:
        return "未勝利"
    return "勝上"


def pace_bias_for(surface, cls_group=None, num_horses=None, straight_home_m=None,
                  distance=None, has_corners=True):
    """レース属性からペース意図バイアス(m/s)を計算する。
    balance空間の実測回帰モデル(クラス+交互作用/頭数/直線長)を距離帯別の
    シミュ応答係数G_BANDで速度単位に変換する。
    - ダートは今回未較正のため0(実測の頭数効果はダートにも存在(r=-0.15〜-0.28)するが、
      会場バイアス問題が診断されたのは芝であり、較正済みのダートS水準を乱さないため
      意図的にスコープ外)。
    - has_corners=False(新潟芝1000等の直線コース)は対象外(0を返す)。コーナーを前提と
      した通常コースの回帰から導いた効果を、構造の全く異なる直線競走に外挿しない。
    """
    if surface != "芝" or not has_corners:
        return 0.0
    is_1600 = bool(distance) and distance < 1800
    bal = 0.0
    if cls_group:
        bal += PACE_BAL_CLS.get(cls_group, 0.0)
        if is_1600:
            bal += PACE_BAL_CLS_1600.get(cls_group, 0.0)
    if num_horses:
        bal += PACE_BAL_K_NH * (num_horses - PACE_BIAS_NH_REF)
    if straight_home_m:
        s = max(PACE_BIAS_STRAIGHT_MIN, min(PACE_BIAS_STRAIGHT_MAX, straight_home_m))
        bal += PACE_BAL_K_STRAIGHT * (s - PACE_BIAS_STRAIGHT_REF)
    if not distance:
        band = "1800"
    elif distance < 1800:
        band = "1600"
    elif distance < 2000:
        band = "1800"
    else:
        band = "2000+"
    b = bal / G_BAND[band]
    return max(-PACE_BIAS_CAP, min(PACE_BIAS_CAP, b))


# --- 道中の上り坂によるペース意図シフト(2026-08-07追加、既定0.0=無効) ------------
# 【背景】ダート短距離セルの実測H率差調査(2026-08-07)で「道中(向正面〜3-4角)に上り坂が
# ある会場(京都・東京)だけ実測H率が低い」と判明。直接物理(k_slope_dirt: 坂の位置で
# 速度を加減算)は上り→下り対の寄与が前後半バランスで相殺され距離で符号が割れて不採用
# (K_SLOPE_DIRTのコメント参照)。本機構はその再設計: 「騎手が道中の上りを見越して
# そもそも控えめに入る」という意図の変化として、pace_bias(先頭馬のP2巡航シフト)と
# 同じ作用点に、レース定数のシフトを加算する。位置依存の物理減速はしない。
# 【設計】道中の上り = 残り SLOPE_INTENT_REMAINING_MIN(500m)以上の地点で「終わる」
# 上り坂ゾーン(course_slope由来)。最終直線の坂(中山/阪神/中京/東京/小倉の既存収録、
# 残り0-520m)は除外 — 最終直線の急坂を持つ中山・阪神の実測H率はむしろ高く、
# 「ゴール前の坂」は序盤の意図を抑えないため。下り坂も含めない — 道中下りの中山で
# ダ1200(実測99.3%)とダ1800(実測35.9%)の方向が割れており「下り=速い意図」は
# データに支持されないため(この2つのスコープ判断は2026-08-07の実測調査の知見に
# 基づく。較正時の係数決定には実測H率を使わない)。
# シフト量 = -coef × (道中上りの総上昇量m)。coef=0.0(既定)で完全レガシー互換。
# 【2026-08-07較正結果: 不採用=0.0維持(直接物理k_slope_dirtに続き2案連続の負の結果)】
# スイープ{0,0.1,0.2,0.3,0.4,0.5}(訓練15ダートセル、n_sim=300、scratchpad/
# slope_intent_sweep.py)で訓練mean|simH-realH|が15.17→19.00ptと単調悪化。
# 【最重要の発見=不発の構造的理由】狙いの京都ダ1200は全係数で100%のまま完全不変だった。
# ダート短距離はdash窓=min(d_c1,400)とkick_trigger=distance-800がほぼ接するため
# P2巡航フェーズが存在せず、pace_bias/pace_noise系(P2巡航の先頭馬に作用)の意図経路が
# 構造的に届かない(pace_noiseが新潟ダ1200に効かないのと同じ機構)。一方、意図が作用する
# 中距離では京都ダ1400(86.7→65.3%、実測92.5%)・京都ダ1800(32.0→3.0%、実測52.4%)と
# シミュが既に冷えすぎのセルをさらに悪化させる(必要な符号が逆)。検証会場は道中上り
# ゾーン非保有で構造的不変、gate2(新潟)も全点不変を確認。
# 【結論】京都/東京ダート短距離の過大Hを直すには、意図系でも位置物理でもなく
# 「ダッシュ/キックフェーズ構造そのもの」(dash窓のd_c1依存・kick開始点)に手を入れる
# 必要がある — d_c1反実仮想(kyoto_da1200_probe.py: 阪神のd_c1移植でH98.3→79.3%)とも
# 整合する帰結。機構はcoef=0.0で完全no-opのため配線ごと温存する。
SLOPE_INTENT_COEF_DIRT = 0.0   # ダート向け係数(m/s per 上昇1m、2026-08-07スイープの結果不採用=0.0)
SLOPE_INTENT_COEF_TURF = 0.0   # 芝向け(未較正・無効。芝のペース系は較正済みのため対象外)
SLOPE_INTENT_REMAINING_MIN = 500.0   # 「道中」判定: 残りこの距離以上で終わる上りのみ算入
SLOPE_INTENT_CAP = 1.2         # 安全上限(m/s)。PACE_BIAS_CAPと同思想


# --- ダート版フェーズ構造: 全力区間を道中の上り坂の入口で打ち切る(2026-08-07追加) ----
# 【背景】京都・東京ダート短距離のシミュH過大(+22〜+40pt)は、3つの独立実験(d_c1反実
# 仮想移植/坂の直接物理k_slope_dirt/坂の意図系slope_intent)が全て「ダッシュ/キックの
# フェーズ構造」に収束した。ダート短距離はdash窓=min(d_c1,400)とkick_trigger=distance-800
# が接するとP2巡航が消滅し、H率が構造的に張り付く。一方、一律の窓短縮では新潟ダ1200
# (実測H98.2%)と京都ダ1200(実測77.7%)を区別できない(両者ともd_c1>400・引込線・1200m)。
# 両者を分ける唯一のモデル可視な入力はcourse_slopeの道中上りゾーン(京都=3角の上り、
# 東京=向正面の上り。JRA公式由来、新潟・中山・阪神・中京は道中上りなし)。
# 【設計】「フル出力のダッシュ・位置取り競合は道中の上り坂の入口で終わる」という一般則。
# 物理的解釈: 上り坂に向かって全力ダッシュは持続できず、騎手はそこで一旦脚を溜める。
# dash窓とpress窓(P1)の両方に適用する — dash窓だけ縮めるとpress窓(kappa_press_dirt=1.2の
# 先頭脅威ブースト+0.96m/s)が残った区間が生じ、かえってH率が上がる逆反応になることを
# 実験で確認済み(2026-08-05のダート除外判断の真因)。
# floor_m: 発走直後の最低全力距離(ゲートダッシュは坂の途中からの発走でも存在する)。
# 唯一の較正パラメータ。
# 【2026-08-07較正・採用】スイープ{legacy, F=0, 100, 150, 200}(訓練15ダートセル、n=300、
# scratchpad/dirt_phase_sweep.py)でF=150が訓練mean|simH-realH|最小(15.17→13.21pt)。
# 影響セルは設計どおり4つのみ: 東京ダ1300 93.3→53.3%(実測53.6%とほぼ一致、+40pt改善)/
# 京都ダ1200 100→91.0%(実測77.7%、+9pt改善)/東京ダ1400 92.3→38.0%(過大→過小に反転、
# 誤差ほぼ不変)/東京ダ1600 71.3→42.3%(実測68.0%、-22pt悪化 — 東京の道中上りゾーン
# 端点(confidence low)が1600mに強く当たりすぎる既知の副作用)。検証会場(函館/札幌/阪神/
# 中京)は道中上りゾーン非保有で全設定不変、gate2(新潟ダ1200)も構造的不変(98.3%)。
# フルゲート: gate1 0.907→0.912(改善)/gate2 98.3%不変/gate3a/3b不変/gate3c OK
# (ダ複数S 4.0→7.2%に上昇、方向性維持)により採用。
# 【残課題】東京1600の悪化は東京ゾーン端点の精度に依存 — 勾配図画像等での再計測が
# 改善候補(H率からの逆算はしないこと)。
DIRT_PHASE_FLOOR_M = 150.0   # 較正済み(2026-08-07、F=150採用)


def dirt_phase_cap(distance, dash_cap_m, slope_zones, floor_m=None):
    """ダート: 全力フェーズ(P0ダッシュ+P1押し合い)の距離上限。道中の上り坂
    (slope_intent_biasと同じ判定: 残りSLOPE_INTENT_REMAINING_MIN以上で終わる上り)の
    入口で打ち切る。坂が無ければdash_cap_mのまま(レガシー一致)。戻り値を
    simulate_field(dash_cap_m=..., press_cap_m=...)の両方に渡すこと(片方だけだと
    逆反応、上記コメント参照)。"""
    if floor_m is None:
        floor_m = DIRT_PHASE_FLOOR_M
    cap = dash_cap_m
    for z in slope_zones or []:
        if z["grade"] > 0 and (distance - z["end"]) >= SLOPE_INTENT_REMAINING_MIN:
            cap = min(cap, max(floor_m, z["start"]))
    return cap


def slope_intent_bias(slope_zones, distance, coef):
    """道中の上り坂に対する先頭馬のペース意図シフト(m/s、負=控えめに入る)。
    slope_zones: build_slope_zones()の戻り値(絶対位置)。coef=0または坂なしで0.0。
    呼び出し側(calibrate_mc_dyn_phase2.run_cell / predict系)がpace_biasに加算して
    simulate_field(pace_bias=...)へ渡す(simulate_field自体は無変更)。"""
    if not coef or not slope_zones:
        return 0.0
    climb = 0.0
    for z in slope_zones:
        if z["grade"] > 0 and (distance - z["end"]) >= SLOPE_INTENT_REMAINING_MIN:
            climb += z["grade"] * (z["end"] - z["start"])
    if climb <= 0:
        return 0.0
    b = -coef * climb
    return max(-SLOPE_INTENT_CAP, min(SLOPE_INTENT_CAP, b))
OVERTAKE_PENALTY_SEC = 0.05   # 直線での追い越しタイムロス(秒/イベント1回。2026-08-06に毎ステップ加算バグを修正、simulate_fieldパス2参照)
CONGESTION_TIME_GAP_S = 0.4   # 混雑判定の到達時間差閾値(秒)
LEADER_THREAT_TIME_S = 0.3    # 単騎逃げ馬が「詰められた」と判断する時間差閾値(秒)
FOLLOWER_EASE_TIME_S = 0.5    # 先頭馬が脚を溜めてよいと判断する後続との時間差閾値(秒)
START_NOISE_SIGMA = 0.3       # P0スタートダッシュの乱数(m/s、レース開始時に馬ごとに1回抽選)
KICK_START_M_P2 = 800.0       # P3(仕掛け)開始距離(残りこの距離から。Phase1のKICK_START_M=600とは別物)
DT_DEFAULT = 0.5              # 複数馬シミュレーションの時間刻み(秒)
MIN_V = 0.5                   # 速度下限(完全停止防止の安全弁、m/s)


def classify_style_simple(pos4, num_horses):
    """pos4(4角通過順位)とnum_horsesから脚質を簡易分類する(単走の実測値のみを使う
    簡易版。generate_race_sim.pyのclassify_style()は過去走履歴の加重平均で分類するため
    同じ馬が「平均的には先行争いに加わるタイプ」でも当該レースのpos4は1つしか無い
    (順位は必ず一意)ため複数頭が「逃げ」になり得るが、単走の実測値だけを使う本関数
    では判定基準を素直に「ratio<0.1」にすると4角順位1位の馬しか該当し得ず
    (num_horses>10でないとpos4=2はratio<0.1を満たさない)、1レースあたり常に
    ちょうど1頭だけが「逃げ」になってしまう(実データで検証: 阪神ダ1800の全レース
    100%が逃げ馬1頭という不自然な分布になった)。これではPhase2仕様が要求する
    「逃げ馬2頭以上の重い先行争い」を含むフィールド構成を実測から再現できない。
    そのため閾値をratio<0.15に広げた(実データで検証: 逃げ馬0頭0.2%/1頭57.4%/
    2頭25.8%/3頭13.9%/4頭以上2.9%という自然な分布になることを確認済み)。
    (仕様書:「またはresults.pos4/num_horsesから簡易分類してもよい」)。"""
    if not pos4 or not num_horses or num_horses <= 1:
        return "先行"
    ratio = pos4 / num_horses
    if pos4 == 1 or ratio < 0.15:
        return "逃げ"
    elif ratio < 0.35:
        return "先行"
    elif ratio < 0.60:
        return "差し"
    else:
        return "追い込み"


def build_corner_zones(distance, circumference, straight_home, arc1, arc2,
                        r1_entry, r1_exit, r2_entry, r2_exit):
    """course_layoutの値(コース1周分の幾何)から、ゴール(=distance)を基準に逆算して
    コーナー区間(複数周ぶん)を構築する。

    ゴール(finish)はホームストレッチ(straight_home)の末端に位置する、という前提で、
    ゴールから逆方向に「ホームストレッチ→corner2→バックストレッチ→corner1」の順に
    circumference周期で繰り返し、[0, distance]にクリップする(distance>circumferenceの
    長距離戦では複数周ぶんのコーナーが現れる)。

    d_c1(発走〜第1コーナー入口、course_start_layout.d_c1_mから取得)とは別の情報源
    であることに注意 — d_c1は「発走位置から見て最初に到達するコーナー」を実測ベースで
    直接算出した値だが、本関数はコーナー速度上限・混雑判定のための「ゴール基準の逆算
    モデル」であり、両者は仕様書上も別用途(P0/P1のフェーズ境界 vs コーナー区間判定)
    として明示的に分離されている。

    戻り値: [{"corner_no", "start", "end", "full_start", "full_end", "r_entry", "r_exit"}, ...]
    (distance方向に昇順。startに満たない区間は除外)。
    """
    backstretch = circumference - straight_home - arc1 - arc2
    if backstretch < 0:
        backstretch = 0.0

    zones = []
    pos = float(distance)
    guard = 0
    while pos > 0 and guard < 100:
        guard += 1
        pos -= straight_home              # ホームストレッチ(コーナーなし)
        if pos <= 0:
            break
        c2_end, c2_start = pos, pos - arc2
        zones.append({"corner_no": 2, "start": max(0.0, c2_start), "end": c2_end,
                       "full_start": c2_start, "full_end": c2_end,
                       "r_entry": r2_entry, "r_exit": r2_exit})
        pos = c2_start
        if pos <= 0:
            break
        pos -= backstretch                 # バックストレッチ(コーナーなし)
        if pos <= 0:
            break
        c1_end, c1_start = pos, pos - arc1
        zones.append({"corner_no": 1, "start": max(0.0, c1_start), "end": c1_end,
                       "full_start": c1_start, "full_end": c1_end,
                       "r_entry": r1_entry, "r_exit": r1_exit})
        pos = c1_start
    # 始端でクリップされた断片(full_start<0)は除外する。これは「このレース距離では
    # 実際には経験しないコーナー」を意味する — ゴール基準の周期モデルは発走ゲートの
    # 実位置(シュート合流・引込線等)を知らないため、生成された最初の周期がたまたま
    # 発走ラインの手前から始まっていた場合、その断片を残すと発走直後を誤って「コーナー
    # 区間」と誤判定してしまう(実際にはd_c1(course_start_layout、実測値)が指す方の
    # コーナーが本当の最初のコーナーであり、この断片はそれではない別のコーナーの残骸)。
    # 検証: 東京芝2000(d_c1実測919.9)はこの除外により残る最初のコーナーが924.1m地点
    # となり実測とほぼ一致する(除外前は誤って0-427mに存在しないコーナー断片を生成していた)。
    zones = [z for z in zones if z["end"] > 0 and z["full_start"] >= -1e-6]
    zones.sort(key=lambda z: z["start"])
    return zones


def find_zone_at(zones, pos):
    """posが含まれるコーナー区間を返す(無ければNone、直線区間)。"""
    for z in zones:
        if z["start"] <= pos < z["end"]:
            return z
    return None


def corner_r_at(zone, pos):
    """コーナー区間内の位置posにおける実効半径R(entry->exit線形補間)。"""
    fs, fe = zone["full_start"], zone["full_end"]
    if fe <= fs:
        return zone["r_entry"]
    frac = (pos - fs) / (fe - fs)
    frac = min(1.0, max(0.0, frac))
    return zone["r_entry"] + (zone["r_exit"] - zone["r_entry"]) * frac


def v_corner_max(R, a_lat=A_LAT):
    """コーナー速度上限 v = sqrt(a_lat * R)。"""
    if R is None or R <= 0:
        return float("inf")
    return math.sqrt(a_lat * R)


def simulate_field(distance, v_base, d_c1, corner_zones, horses, q_star,
                    k0=K0, phi_fade=PHI_FADE,
                    kappa_press=KAPPA_PRESS, k_gap=K_GAP, rho_save=RHO_SAVE, a_lat=A_LAT,
                    press_cap_m=PRESS_CAP_M, dash_cap_m=DASH_CAP_M, dash_window_m=None,
                    target_gap_m=TARGET_GAP_M, d_scale=D_SCALE,
                    dash_min_frac=DASH_MIN_FRAC, dash_rival_sat=DASH_RIVAL_SAT,
                    is_chute_start=False, chute_dash_frac=CHUTE_DASH_FRAC,
                    overtake_penalty_sec=OVERTAKE_PENALTY_SEC,
                    congestion_gap_s=CONGESTION_TIME_GAP_S,
                    leader_threat_s=LEADER_THREAT_TIME_S,
                    follower_ease_s=FOLLOWER_EASE_TIME_S,
                    solo_ease_scale=SOLO_EASE_SCALE, ease_rival_sat=EASE_RIVAL_SAT,
                    nige_settle_prob=NIGE_SETTLE_PROB,
                    pace_noise_sigma=PACE_NOISE_SIGMA, pace_bias=0.0,
                    start_noise_sigma=START_NOISE_SIGMA,
                    kick_start_m=KICK_START_M_P2,
                    slope_zones=None, k_slope=K_SLOPE, track_cond_factor=1.0,
                    dt=DT_DEFAULT, max_time=400.0, seed=None,
                    record_snapshots=False):
    """複数馬フィールドシミュレーション(Phase2: 戦術コントローラP0-P4)。

    horses: [{"style": "逃げ"|"先行"|"差し"|"追い込み", "spd":80.0, "spr":80.0, "sta":75.0}, ...]
    q_star: {"逃げ":.., "先行":.., "差し":.., "追い込み":..}  (pos1/num_horses実測分位点)
    corner_zones: build_corner_zones()の戻り値
    d_c1: course_start_layout.d_c1_m (P0/P1のフェーズ境界専用)

    戻り値: dict(finish_times, order, leader_laps, seg_lens, leader_total_time, styles)
    leader_lapsはfetch_laps.py:calc_derived()にそのまま渡せる区間ラップ秒列
    (各セグメント境界にフィールド最速で到達した時刻の差分= 実際のレース既定と同じ
    「先頭の通過タイム」ベースのラップ)。

    record_snapshots=True(オプトイン、2026-08-03追加)の場合のみ、戻り値に
    "snapshots" を追加する: 各チェックポイント(phase_c1=d_c1 / phase_kick=kick_trigger /
    zone{i}_in・zone{i}_out=各コーナー区間の入口・出口 / goal=ゴール)を各馬が通過した
    時刻(dt内線形補間)と、その通過時刻順の順位(1始まり、未到達馬は最後尾・index順)。
    Falseの時は記録処理を一切行わず、従来と完全に同一の動作・戻り値
    (乱数消費・浮動小数点演算とも不変。baseline_simfield.pyで24ケース完全一致を確認済み)。
    """
    rng = random.Random(seed)
    n = len(horses)
    if n == 0:
        return {"finish_times": [], "order": [], "leader_laps": [], "seg_lens": [],
                "leader_total_time": None, "styles": []}

    d_c1 = d_c1 or 0.0
    final_corner = corner_zones[-1] if corner_zones else None
    # dash_window_m(2026-08-05追加): Noneならレガシー(第1コーナーとcapの近い方で
    # ダッシュ終了)、数値なら d_c1非依存の固定窓(芝で採用、dash_window_for()参照)。
    if dash_window_m is None:
        dash_end = min(d_c1, dash_cap_m)
    else:
        dash_end = min(float(distance), dash_window_m)
    # 【2026-08-02修正】最終コーナー「入口(start)」ではなく「出口(end)」を仕掛けトリガーの
    # 代替条件にする。のりお指摘で発覚: 東京は最終コーナー+直線の合計距離が実距離として
    # ほぼ一定(残り1,000m前後)なため、短距離レースほどレース全体に占めるこの絶対距離の
    # 割合が大きくなり、「コーナー入口」を基準にすると短距離レースで極端に早い地点
    # (東京芝1400mでレースの23%地点!)で仕掛け開始と誤判定していた(実測: 誤判定の
    # 早さと予測誤差がほぼ単調な関係、東京9セルで確認)。実際の騎手はコーナーの途中では
    # なく直線に向いてから仕掛けるため、コーナー「出口」を基準にする方が実態に近い。
    kick_trigger = min(distance - kick_start_m, final_corner["end"] if final_corner else distance)

    # スナップショット記録(オプトイン)。checkpoints/cross_timesはFalse時は未使用。
    checkpoints = None
    if record_snapshots:
        checkpoints = [("phase_c1", min(d_c1, float(distance))),
                       ("phase_kick", float(kick_trigger))]
        for zi, z in enumerate(corner_zones):
            checkpoints.append((f"zone{zi}_in", float(z["start"])))
            checkpoints.append((f"zone{zi}_out", min(float(z["end"]), float(distance))))
        checkpoints.append(("goal", float(distance)))
        cp_dists = [c[1] for c in checkpoints]
        cross_times = [[None] * len(checkpoints) for _ in range(len(horses))]

    # 脚質ごとの頭数(ダッシュのライバル数スケール用)
    style_counts = {}
    for h in horses:
        st = h.get("style", "先行")
        style_counts[st] = style_counts.get(st, 0) + 1

    # --- 複数逃げの先導権決着(2026-08-07追加、NIGE_SETTLE_PROBのコメント参照) -------
    # 決着した場合、残す1頭以外の逃げ馬を「戦術脚質=先行」として扱う(tact_styles)。
    # 以降の戦術ロジック(ダッシュ・ライバル数・nige_idxs・P1・q_star目標)は全て
    # tact_stylesを参照し、出力のstylesのみ入力の脚質を保持する。
    # 乱数消費: prob=0または逃げ<2頭では一切消費しない(レガシーとビット単位一致)。
    # prob>0かつ逃げ>=2頭では決着の成否に関わらず常に2回消費する(random+randrange)
    # ことで、較正スイープでprobだけを変えても馬能力・ペースノイズがCRNで固定される。
    tact_styles = [h.get("style", "先行") for h in horses]
    if nige_settle_prob > 0:
        _nige_in = [i for i, st in enumerate(tact_styles) if st == "逃げ"]
        if len(_nige_in) >= 2:
            _settle = rng.random() < nige_settle_prob
            _keep = _nige_in[rng.randrange(len(_nige_in))]
            if _settle:
                tact_styles = [("先行" if (st == "逃げ" and i != _keep) else st)
                               for i, st in enumerate(tact_styles)]

    state = []
    for idx, h in enumerate(horses):
        spd_res = h.get("spd", 80.0) - 80.0
        spr_res = h.get("spr", 80.0) - 80.0
        sta_res = h.get("sta", 75.0) - 75.0
        style = tact_styles[idx]
        e_max = E0 + BE * sta_res

        # ダッシュの競争依存スケール: 同格以上に積極的な脚質のライバル数(自分を除く)を数え、
        # 0(単騎)ならdash_min_frac、dash_rival_sat以上でフル(1.0)に線形補間する。
        # (2026-08-07以降は戦術脚質tact_stylesで数える。prob=0なら入力脚質と同一)
        my_agg = STYLE_AGGRESSION.get(style, 1)
        n_rivals = sum(1 for j2 in range(n) if j2 != idx
                       and STYLE_AGGRESSION.get(tact_styles[j2], 1) <= my_agg)
        contest = min(1.0, n_rivals / dash_rival_sat) if dash_rival_sat > 0 else 1.0
        dash_frac = dash_min_frac + (1.0 - dash_min_frac) * contest
        if is_chute_start:
            # 引込線発走: ライバル数に関わらず発走地点そのものが先行争いを激化させるため
            # ダッシュ割合の下限を底上げする(ライバル多数で既にdash_frac>chute_dash_fracの
            # 場合はそのまま、通常発走より弱まることはない)。
            dash_frac = max(dash_frac, chute_dash_frac)

        state.append({
            "style": style, "spr_res": spr_res,
            "v_flat": (v_base + spd_res * A1) * track_cond_factor,
            "dash": d_scale * D_STYLE.get(style, D_STYLE["先行"]) * dash_frac,
            "noise": rng.gauss(0.0, start_noise_sigma),
            "E": e_max, "pos": 0.0, "v": 0.0, "t": 0.0,
            "finished": False, "finish_time": None,
        })

    nige_idxs = [i for i, s in enumerate(state) if s["style"] == "逃げ"]

    # 構成依存イージング(単騎逃げの余裕): レース構成は不変なので1回だけ計算する。
    # solo_ease_scale=0.0(既定)ならcomp_ease=0.0となり従来動作とビット単位で一致。
    if ease_rival_sat > 0:
        _solo_f = max(0.0, 1.0 - max(0, len(nige_idxs) - 1) / ease_rival_sat)
    else:
        _solo_f = 0.0
    comp_ease = rho_save * solo_ease_scale * _solo_f

    # レースレベルのペース意図ノイズ(1レース1回抽選、P2巡航の先頭馬にのみ加算)。
    # sigma=0.0(既定)では抽選せず乱数ストリームを消費しない=レガシーとビット単位一致。
    # pace_bias(2026-08-05追加): レース属性(クラス・頭数・直線長)由来の決定論的シフト。
    # 呼び出し側がpace_bias_for()で計算して渡す。既定0.0でレガシーとビット単位一致
    # (float加算 x+0.0 はビットパターン不変)。
    pace_noise = (rng.gauss(0.0, pace_noise_sigma) if pace_noise_sigma > 0 else 0.0) + pace_bias

    n_seg = max(3, round(distance / 200))
    seg_lens = segment_lengths(n_seg, distance)
    markers = []
    acc = 0.0
    for sl in seg_lens:
        acc += sl
        markers.append(acc)
    marker_hit_times = [None] * len(markers)

    # 追い越しペナルティの課金済みペア(i→jで1回課金したら再課金しない。パス4参照、
    # 2026-08-06追加)。ペナルティ無効(=0)なら確保しない。
    passed_already = [set() for _ in range(n)] if overtake_penalty_sec else None

    max_steps = int(max_time / dt) + 1
    for _ in range(max_steps):
        if all(s["finished"] for s in state):
            break

        order_idx = sorted(range(n), key=lambda i: (-state[i]["pos"], -state[i]["v"]))
        rank_of = {idx: r for r, idx in enumerate(order_idx)}

        # --- パス1: フェーズ別v_des決定 -------------------------------------
        v_des_list = [None] * n
        for i, s in enumerate(state):
            if s["finished"]:
                continue
            pos = s["pos"]
            v_flat = s["v_flat"]

            if pos < kick_trigger:
                if pos < dash_end:
                    # P0 スタートダッシュ
                    v_des = v_flat + s["dash"] + s["noise"]
                else:
                    # P2 巡航
                    r = rank_of[i]
                    if r == 0:
                        if n > 1:
                            follower_idx = order_idx[1]
                            gap_m = pos - state[follower_idx]["pos"]
                            follower_v = state[follower_idx]["v"] or v_flat
                            gap_s = gap_m / follower_v if follower_v > 0 else 999.0
                        else:
                            gap_s = 999.0
                        # gap基準の脚溜め(レガシー)と構成依存イージングの大きい方を適用
                        gap_ease = 0.0 if gap_s < follower_ease_s else rho_save
                        v_des = v_flat - (gap_ease if gap_ease >= comp_ease else comp_ease) \
                            + pace_noise
                    else:
                        ahead_idx = order_idx[r - 1]
                        gap_m = state[ahead_idx]["pos"] - pos
                        ahead_v = state[ahead_idx]["v"] or v_flat
                        v_des = ahead_v + k_gap * (gap_m - target_gap_m)

                # P1 位置取り競合(発走〜min(D_c1,press_cap_m)のオーバーレイ、P0/P2の基準速度に加算)
                if pos < min(d_c1, press_cap_m):
                    r = rank_of[i]
                    rank_ratio = (r + 1) / n
                    target = q_star.get(s["style"], q_star.get("先行", 0.3))
                    rank_gap = max(0.0, rank_ratio - target)
                    v_des += kappa_press * min(1.0, rank_gap)
                    if s["style"] == "逃げ" and len(nige_idxs) >= 2 and r != 0:
                        v_des += kappa_press
                    if s["style"] == "逃げ" and r == 0 and n > 1:
                        follower_idx = order_idx[1]
                        gap_m = pos - state[follower_idx]["pos"]
                        follower_v = state[follower_idx]["v"] or v_flat
                        gap_s = gap_m / follower_v if follower_v > 0 else 999.0
                        if gap_s <= leader_threat_s:
                            v_des += 0.8 * kappa_press
                s["_in_kick"] = False
            else:
                # P3/P4 仕掛け・直線: Phase1のkick/energy式そのまま
                v_des = v_flat + (k0 + K1 * s["spr_res"]) * min(1.0, s["E"] / E_REF)
                if s["E"] <= 0:
                    v_des = min(v_des, v_flat * (1 - phi_fade))
                s["_in_kick"] = True

            grade = find_slope_grade(slope_zones, pos)
            if grade:
                v_des = max(MIN_V, v_des - k_slope * grade)

            zone = find_zone_at(corner_zones, pos)
            if zone is not None:
                v_des = min(v_des, v_corner_max(corner_r_at(zone, pos), a_lat))

            v_des_list[i] = max(v_des, MIN_V)

        # --- パス2: 混雑(直前馬との到達時間差<0.4秒、コーナー内は前が壁) ---------
        # 【2026-08-06修正】旧実装はこのパスの直線分岐で s["t"] += overtake_penalty_sec を
        # 毎ステップ加算していた。渋滞条件(gap_s<congestion_gap_s)はP2追走コントローラが
        # 車間をtarget_gap_m=3.0m≈0.18秒<0.4秒に収束させるため慢性的に成立し続け、
        # 0.5秒刻みの全ステップで0.05秒が課金され続けていた(1レース約500回)。この結果、
        # (1)ペナルティ総量がステップ数∝1/dtに比例し、シミュレーションがdtに収束しない
        # (2)スタミナ増で隊列内滞在が延びてかえって遅くなる逆符号の挙動
        # (3)s["t"]汚染がmarker_hit_times経由でpace_type判定まで歪める、という3つの
        # 不具合が発生していた。修正後、追い越しペナルティは「実際に追い抜きが完了した
        # イベント」に対して1回だけ課金する方式(パス4、位置更新後の交差検出)に移した。
        # このパスに残るのはコーナー内の「前が壁」(速度キャップ)のみ(従来と同一)。
        for i, s in enumerate(state):
            if s["finished"] or v_des_list[i] is None:
                continue
            r = rank_of[i]
            if r == 0:
                continue
            ahead_idx = order_idx[r - 1]
            if state[ahead_idx]["finished"]:
                continue
            gap_m = state[ahead_idx]["pos"] - s["pos"]
            ahead_v = state[ahead_idx]["v"] or s["v_flat"]
            gap_s = gap_m / ahead_v if ahead_v > 0 else 999.0
            if gap_s < congestion_gap_s and v_des_list[i] > (state[ahead_idx]["v"] or 0.0):
                zone = find_zone_at(corner_zones, s["pos"])
                if zone is not None:
                    v_des_list[i] = min(v_des_list[i], state[ahead_idx]["v"])

        # 追い越し検出(パス4)用の移動前位置スナップショット。ペナルティ無効(=0)なら
        # 記録もペア走査も行わない(コスト・挙動ともレガシーのpen=0.0と完全一致)。
        pos_before = [s["pos"] for s in state] if overtake_penalty_sec else None

        # --- パス3: 位置・エネルギー更新 ----------------------------------------
        for i, s in enumerate(state):
            if s["finished"]:
                continue
            v = v_des_list[i]
            step_dist = v * dt
            if record_snapshots:
                _pos_before, _t_before = s["pos"], s["t"]
            if s["pos"] + step_dist >= distance:
                remain = distance - s["pos"]
                frac_t = remain / v if v > 0 else 0.0
                s["t"] += frac_t
                s["pos"] = distance
                s["finished"] = True
                s["finish_time"] = s["t"]
                s["v"] = v
            else:
                # エネルギーは仕掛け(P3/P4、Phase1のkick式)フェーズでのみ消費する。
                # P0(スタートダッシュ)/P1(位置取り競合)の超過速度は「同じEを共有する
                # 別の(anaerobicな)瞬発力」とみなし、末脚用の予算(Phase1でe_max=250と
                # 較正済み、もともと残り600m程度の仕掛けにのみ充当する想定の値)は削らない。
                # 検証時にP0/P1もEを消費する実装を試したところ、d_c1が長いコース(400-900m)
                # でe_maxを大幅に超過するペースで枯渇し(実測でe_max=250に対し15秒で
                # 500超消費を確認)、あらゆるセルでシミュがH判定に張り付く(実測との相関が
                # 崩壊する)不具合を確認したため、この切り分けを採用した。
                if s["_in_kick"] and v > s["v_flat"]:
                    s["E"] -= (v - s["v_flat"]) * step_dist
                s["pos"] += step_dist
                s["t"] += dt
                s["v"] = v
            for mi, mpos in enumerate(markers):
                if marker_hit_times[mi] is None and s["pos"] >= mpos:
                    marker_hit_times[mi] = s["t"]
            if record_snapshots:
                # チェックポイント通過時刻(dt内線形補間、状態・乱数には一切触れない)
                for ci, cd in enumerate(cp_dists):
                    if cross_times[i][ci] is None and s["pos"] >= cd:
                        if v > 0 and s["pos"] > _pos_before:
                            cross_times[i][ci] = _t_before + (cd - _pos_before) / v
                        else:
                            cross_times[i][ci] = s["t"]

        # --- パス4: 追い越しイベント検出・ペナルティ(2026-08-06追加、パス2のコメント参照) ---
        # 「追い越しイベント1回につき1回」の課金。移動前後の位置比較で実際に追い抜きが
        # 完了したペア(軌道の交差)を検出し、同一ペア(i→j)への課金は1レース1回までとする。
        # 【設計の経緯(いずれも検証済み)】
        #  - 旧実装(渋滞中毎ステップ課金): ペナルティ総量∝1/dtでdt非収束(本修正の対象バグ)
        #  - 中間案1(渋滞突入時に1回のフラグ方式): dtが細かいほど順位入れ替わりの検知が
        #    細粒度になり「ブロック相手の変化」イベント数が膨らむ残留dt依存で不採用
        #  - 中間案2(交差1回ごとに課金・ペアメモリなし): kappa_pressの逃げライバル
        #    ブースト(先頭でない逃げ馬に+kappa)がon/off制御のため、押し合い区間で
        #    隣接ペアがチャタリング(抜く→ブースト消滅→抜き返される→...)を起こし、
        #    交差回数そのものが連続極限で発散する(ダートのNG12/HS18でdt=0.5→0.125の
        #    H率が99→76%等)ため不採用。ペアメモリで「デュエルの入れ替わり往復」を
        #    1イベントに正規化するとdt収束する。
        # 順位入れ替わりが無いステップはソート1回の比較だけでスキップする(大半のステップ)。
        # コーナー内で完了した追い抜き(パス2の壁により原則発生しないが、2つ以上前の馬への
        # 多段追い抜きで稀に起きる)は従来仕様(直線のみ課金)を踏襲し課金しない
        # (課金しなかったペアはpassed_alreadyに登録せず、後で直線で改めて抜けば課金される)。
        if overtake_penalty_sec:
            order_after = sorted(range(n), key=lambda k: -state[k]["pos"])
            if order_after != order_idx:
                for i, s in enumerate(state):
                    pb_i, pa_i = pos_before[i], s["pos"]
                    if pa_i <= pb_i:
                        continue   # このステップで前進していない(既ゴール等)
                    newly = [j for j in range(n)
                             if j != i and pos_before[j] > pb_i and state[j]["pos"] < pa_i
                             and j not in passed_already[i]]
                    if newly:
                        # 交差地点の代表として自馬の移動区間中点で直線/コーナーを判定
                        if find_zone_at(corner_zones, (pb_i + pa_i) / 2) is None:
                            passed_already[i].update(newly)
                            s["t"] += overtake_penalty_sec * len(newly)
                            if s["finished"]:
                                s["finish_time"] = s["t"]

    finish_times = [s["finish_time"] if s["finish_time"] is not None else s["t"] for s in state]
    order = sorted(range(n), key=lambda i: finish_times[i])

    leader_laps = []
    prev = 0.0
    for mt in marker_hit_times:
        if mt is None:
            mt = prev
        leader_laps.append(max(mt - prev, 1e-3))
        prev = mt

    result = {
        "finish_times": finish_times,
        "order": order,
        "leader_laps": leader_laps,
        "seg_lens": seg_lens,
        "leader_total_time": marker_hit_times[-1] if marker_hit_times and marker_hit_times[-1] is not None else prev,
        # 出力は入力の脚質を保持する(先導権決着の戦術脚質tact_stylesは内部処理のみ。
        # prob=0ならtact_styles==入力脚質なので従来と同一)。
        "styles": [h.get("style", "先行") for h in horses],
    }
    if record_snapshots:
        ranks = {}
        for ci, (name, _cd) in enumerate(checkpoints):
            ts = [cross_times[i][ci] for i in range(n)]
            order_cp = sorted(range(n),
                              key=lambda i: (ts[i] is None,
                                             ts[i] if ts[i] is not None else 0.0, i))
            rk = [0] * n
            for r_, i in enumerate(order_cp):
                rk[i] = r_ + 1
            ranks[name] = rk
        result["snapshots"] = {
            "checkpoints": [{"name": nm, "dist": dv} for nm, dv in checkpoints],
            "cross_times": cross_times,
            "ranks": ranks,
        }
    return result
