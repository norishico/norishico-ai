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
K_SLOPE = 0.0     # 勾配(rise/run)1あたりの速度減算量(m/s)。座標降下法で調整対象、上記の理由で0採用


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
        if distance < remaining_start_m:
            continue
        zones.append({"start": distance - remaining_start_m,
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
         slope_zones=None, k_slope=K_SLOPE):
    """レール上1頭走行シミュレーション。戻り値: (総タイム, 区間ラップ秒のリスト)。
    発走直後(accel_zone_m以内)はスタンディングスタートの加速により速度を割り引く。"""
    e_max = E0 + BE * sta_res
    E = e_max
    cum = 0.0
    laps = []
    v_flat = v_base + spd_res * A1
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
CHUTE_DASH_FRAC = 0.9     # 引込線発走時のダッシュ割合の下限(座標降下法で調整対象、2026-08-01較正済み値。
                          # ダート限定適用に変更済み、calibrate_mc_dyn_phase2.pyのapply_chute_boost参照)

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

# --- モデル化のための設計判断(仕様書に数値指定が無いため明記する既定値) -------
TARGET_GAP_M = 3.0        # P2追走時の目標車間(m、おおよそ1馬身)
OVERTAKE_PENALTY_SEC = 0.05   # 直線での追い越しタイムロス(秒)
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
                    target_gap_m=TARGET_GAP_M, d_scale=D_SCALE,
                    dash_min_frac=DASH_MIN_FRAC, dash_rival_sat=DASH_RIVAL_SAT,
                    is_chute_start=False, chute_dash_frac=CHUTE_DASH_FRAC,
                    overtake_penalty_sec=OVERTAKE_PENALTY_SEC,
                    congestion_gap_s=CONGESTION_TIME_GAP_S,
                    leader_threat_s=LEADER_THREAT_TIME_S,
                    follower_ease_s=FOLLOWER_EASE_TIME_S,
                    start_noise_sigma=START_NOISE_SIGMA,
                    kick_start_m=KICK_START_M_P2,
                    slope_zones=None, k_slope=K_SLOPE,
                    dt=DT_DEFAULT, max_time=400.0, seed=None):
    """複数馬フィールドシミュレーション(Phase2: 戦術コントローラP0-P4)。

    horses: [{"style": "逃げ"|"先行"|"差し"|"追い込み", "spd":80.0, "spr":80.0, "sta":75.0}, ...]
    q_star: {"逃げ":.., "先行":.., "差し":.., "追い込み":..}  (pos1/num_horses実測分位点)
    corner_zones: build_corner_zones()の戻り値
    d_c1: course_start_layout.d_c1_m (P0/P1のフェーズ境界専用)

    戻り値: dict(finish_times, order, leader_laps, seg_lens, leader_total_time, styles)
    leader_lapsはfetch_laps.py:calc_derived()にそのまま渡せる区間ラップ秒列
    (各セグメント境界にフィールド最速で到達した時刻の差分= 実際のレース既定と同じ
    「先頭の通過タイム」ベースのラップ)。
    """
    rng = random.Random(seed)
    n = len(horses)
    if n == 0:
        return {"finish_times": [], "order": [], "leader_laps": [], "seg_lens": [],
                "leader_total_time": None, "styles": []}

    d_c1 = d_c1 or 0.0
    final_corner = corner_zones[-1] if corner_zones else None
    dash_end = min(d_c1, 400.0)
    kick_trigger = min(distance - kick_start_m, final_corner["start"] if final_corner else distance)

    # 脚質ごとの頭数(ダッシュのライバル数スケール用)
    style_counts = {}
    for h in horses:
        st = h.get("style", "先行")
        style_counts[st] = style_counts.get(st, 0) + 1

    state = []
    for h in horses:
        spd_res = h.get("spd", 80.0) - 80.0
        spr_res = h.get("spr", 80.0) - 80.0
        sta_res = h.get("sta", 75.0) - 75.0
        style = h.get("style", "先行")
        e_max = E0 + BE * sta_res

        # ダッシュの競争依存スケール: 同格以上に積極的な脚質のライバル数(自分を除く)を数え、
        # 0(単騎)ならdash_min_frac、dash_rival_sat以上でフル(1.0)に線形補間する。
        my_agg = STYLE_AGGRESSION.get(style, 1)
        n_rivals = sum(1 for h2 in horses if h2 is not h
                       and STYLE_AGGRESSION.get(h2.get("style", "先行"), 1) <= my_agg)
        contest = min(1.0, n_rivals / dash_rival_sat) if dash_rival_sat > 0 else 1.0
        dash_frac = dash_min_frac + (1.0 - dash_min_frac) * contest
        if is_chute_start:
            # 引込線発走: ライバル数に関わらず発走地点そのものが先行争いを激化させるため
            # ダッシュ割合の下限を底上げする(ライバル多数で既にdash_frac>chute_dash_fracの
            # 場合はそのまま、通常発走より弱まることはない)。
            dash_frac = max(dash_frac, chute_dash_frac)

        state.append({
            "style": style, "spr_res": spr_res,
            "v_flat": v_base + spd_res * A1,
            "dash": d_scale * D_STYLE.get(style, D_STYLE["先行"]) * dash_frac,
            "noise": rng.gauss(0.0, start_noise_sigma),
            "E": e_max, "pos": 0.0, "v": 0.0, "t": 0.0,
            "finished": False, "finish_time": None,
        })

    nige_idxs = [i for i, s in enumerate(state) if s["style"] == "逃げ"]

    n_seg = max(3, round(distance / 200))
    seg_lens = segment_lengths(n_seg, distance)
    markers = []
    acc = 0.0
    for sl in seg_lens:
        acc += sl
        markers.append(acc)
    marker_hit_times = [None] * len(markers)

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
                        v_des = v_flat if gap_s < follower_ease_s else (v_flat - rho_save)
                    else:
                        ahead_idx = order_idx[r - 1]
                        gap_m = state[ahead_idx]["pos"] - pos
                        ahead_v = state[ahead_idx]["v"] or v_flat
                        v_des = ahead_v + k_gap * (gap_m - target_gap_m)

                # P1 位置取り競合(発走〜D_c1のオーバーレイ、P0/P2の基準速度に加算)
                if pos < d_c1:
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

        # --- パス2: 混雑・追い越し(直前馬との到達時間差<0.4秒) ------------------
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
                else:
                    s["t"] += overtake_penalty_sec

        # --- パス3: 位置・エネルギー更新 ----------------------------------------
        for i, s in enumerate(state):
            if s["finished"]:
                continue
            v = v_des_list[i]
            step_dist = v * dt
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

    finish_times = [s["finish_time"] if s["finish_time"] is not None else s["t"] for s in state]
    order = sorted(range(n), key=lambda i: finish_times[i])

    leader_laps = []
    prev = 0.0
    for mt in marker_hit_times:
        if mt is None:
            mt = prev
        leader_laps.append(max(mt - prev, 1e-3))
        prev = mt

    return {
        "finish_times": finish_times,
        "order": order,
        "leader_laps": leader_laps,
        "seg_lens": seg_lens,
        "leader_total_time": marker_hit_times[-1] if marker_hit_times and marker_hit_times[-1] is not None else prev,
        "styles": [s["style"] for s in state],
    }
