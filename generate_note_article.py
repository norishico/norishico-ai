"""weekend_predictions.jsonからYukiキャラのnote記事+X投稿を生成
土曜版/日曜版を両方出力
"""
import json, sys, io
from datetime import datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

preds = json.load(open('weekend_predictions.json', encoding='utf-8'))

def get_day(race_id):
    """race_idの日目(8-9桁目)が奇数なら土曜、偶数なら日曜"""
    day_num = int(race_id[8:10])
    return 'sat' if day_num % 2 == 1 else 'sun'

def get_date_label(day):
    """weekend_predictions.jsonの実データから日付ラベルを生成"""
    for p in preds:
        rid = p['race']['race_id']
        if get_day(rid) == day:
            # race_idからYYYYを取得し、race情報のstart_timeなどから日付推定
            # race_data2やrace_detailにある日付情報を使う
            pass
    # フォールバック: race_idから計算
    from datetime import datetime, timedelta
    today = datetime.now()
    wd = today.weekday()
    if wd <= 4:
        sat = today + timedelta(days=(5 - wd))
    elif wd == 5:
        sat = today
    else:
        sat = today - timedelta(days=1)
    sun = sat + timedelta(days=1)
    dt = sat if day == 'sat' else sun
    return f"{dt.month}/{dt.day}（{'土' if day == 'sat' else '日'}）"

buys = [p for p in preds if p['buy_type'] or p['special_horse']]

# 日ごとに分ける
sat_buys = [p for p in buys if get_day(p['race']['race_id']) == 'sat']
sun_buys = [p for p in buys if get_day(p['race']['race_id']) == 'sun']

# 重要度順にソート
def sort_key(p):
    rank = {'v6_star3': 0, 'v6_star2': 1, 'v6_challenge': 2}
    return rank.get(p['buy_type'], 3)

sat_buys.sort(key=sort_key)
sun_buys.sort(key=sort_key)

# A: 血統キャラ辞書(主要産駒の短文キャラ)
SIRE_CHARS = {
    "ディープインパクト": "末脚一閃の王道血統",
    "キングカメハメハ": "距離対応が幅広い堅実型",
    "ハーツクライ": "晩成差し脚で追い比べに強い",
    "ロードカナロア": "短距離のスピード血統",
    "オルフェーヴル": "ムラあるがハマれば一発の爆発タイプ",
    "キズナ": "芝ダ兼用、父ディープの末脚譲り",
    "ダンカーク": "ダート短距離で無類の強さ",
    "ベンバトル": "重馬場巧者の海外血統",
    "ヴァンセンヌ": "ダートで一発ある穴血統",
    "ジャスタウェイ": "ダート中距離の底力型",
    "ダノンレジェンド": "ダート短距離のスペシャリスト",
    "ブラックタイド": "晩成型ダート寄りの渋い血統",
    "スクリーンヒーロー": "ダートで堅実に走る",
    "アジアエクスプレス": "ダート特化の再生系",
    "アメリカンペイトリオット": "芝適性だが人気薄で妙味",
    "American Pharoah": "海外G1級のダート血統",
    "ディーマジェスティ": "条件ハマった時の爆発力",
    "タリスマニック": "欧州芝血統の持続力型",
    "アルアイン": "中距離の堅実派",
    "フィエールマン": "長距離型の新世代",
    "マジェスティックウォリアー": "ダート中距離で根強い",
    "シニスターミニスター": "ダート短距離の実績派",
    "キタサンブラック": "中距離王道・重賞実績型",
    "グレーターロンドン": "新興血統の底力未知数",
    "ハービンジャー": "芝中距離で堅実な持続力",
    "モーリス": "中距離の先行好位差し",
    "ルーラーシップ": "パワー型の芝ダ兼用",
    "エピファネイア": "勝負強さが売りの中距離",
    "ドレフォン": "ダート短距離の新鋭",
    "ヘニーヒューズ": "ダートのスピード型",
}

# C: 文末バリエーション(シャッフルで繰り返し回避)
ENDINGS = [
    "要注目です。",
    "狙って損なしの1頭。",
    "見逃せません。",
    "今日の一撃候補。",
    "じっくり押さえたい。",
    "妙味あるゾーンです。",
    "抑えておきたい存在。",
    "チェック必須の馬。",
    "馬券に絡んでも不思議ない。",
    "今日の中で異色の1頭。",
]


def _pick_angle(hp, best, idx):
    """B: 切り口分散"""
    odds = hp.get('odds', 0) or 0
    try:
        pop = int(str(hp.get('popularity', '0')).replace('**', '0') or '0')
    except Exception:
        pop = 0
    roi = best['roi']
    n = best['n']
    # 特殊条件優先
    if pop >= 10 or odds >= 20:
        return 'odds_gap'  # 人気薄妙味
    if n >= 150:
        return 'sample'  # サンプル厚み
    if roi >= 300:
        return 'value'  # ROI爆発
    return ['blood', 'cond', 'blood', 'value'][idx % 4]


def _yuki_comment(hp, best, idx=0, endings_used=None):
    """Yukinote風 紹介コメント (★数で文量+切り口+文末 分散)"""
    if endings_used is None:
        endings_used = []
    desc_parts = best['desc'].split('×')
    sire = desc_parts[0]
    conds = desc_parts[1:] if len(desc_parts) > 1 else []
    cond_text = '×'.join(conds) if conds else ''
    roi = best['roi']
    n = best['n']
    odds = hp.get('odds', 0) or 0
    venue = hp['venue']
    char = SIRE_CHARS.get(sire, '')
    angle = _pick_angle(hp, best, idx)

    # 文末を未使用から選択(枯れたらリセット)
    available = [e for e in ENDINGS if e not in endings_used]
    if not available:
        endings_used.clear()
        available = ENDINGS[:]
    ending = available[idx % len(available)]
    endings_used.append(ending)

    # 本文生成(切り口別)
    if angle == 'blood':
        if char:
            body = f"{sire}は{char}。{cond_text}でn={n}・単回{roi:.0f}%の実績。"
        else:
            body = f"{sire}×{cond_text}でn={n}・単回{roi:.0f}%、データ上の優位あり。"
    elif angle == 'cond':
        body = f"{cond_text}の条件は{sire}にハマりやすい。n={n}で単回{roi:.0f}%と結果を残している。"
    elif angle == 'odds_gap':
        body = f"想定{odds:.1f}倍は人気薄だが、{sire}のこの条件は過去n={n}で単回{roi:.0f}%。妙味ゾーン。"
    elif angle == 'sample':
        body = f"n={n}と十分なサンプル数。{sire}×{cond_text}は統計的に安定して単回{roi:.0f}%を記録。"
    elif angle == 'value':
        body = f"単回{roi:.0f}%は爆発力のあるゾーン。{sire}×{cond_text}でn={n}件の裏付け。"
    else:
        body = f"{sire}×{cond_text}、n={n}件で単回{roi:.0f}%。"

    text = body + ending

    # ★★★ は追加の一言
    if hp['best_conf'] >= 3:
        extras = [
            f"今日の{venue}でも目を引くデータ。",
            "複数条件で裏付けされた強い根拠。",
            "このクラスのROIは簡単に出ない数字。",
            "統計的な信頼感は抜けて高いゾーン。",
            "サンプル・ROIともに揃った堅めパターン。",
        ]
        text += extras[idx % len(extras)]
    elif hp['best_conf'] == 1:
        # ★ は少し軽めに縮める(1文以内)
        pass
    return text


def generate_article(day_buys, day, date_label):
    """1日分のnote記事を生成 (Yukinote風 注目データ紹介に特化)"""
    all_day_preds = [p for p in preds if get_day(p['race']['race_id']) == day]
    day_hotspots = []
    for p in all_day_preds:
        for hp in p.get('hotspot_picks', []):
            day_hotspots.append(hp)
    # 会場順 → レース番号順でソート
    _venue_order = {'札幌':0,'函館':1,'福島':2,'新潟':3,'東京':4,'中山':5,'中京':6,'京都':7,'阪神':8,'小倉':9}
    day_hotspots.sort(key=lambda x: (_venue_order.get(x['venue'], 99), x['race_num']))

    if not day_hotspots:
        return ""

    # 挨拶
    g1_name = next((p['race']['race_name'] for p in day_buys if p.get('grade') == 'G1'), None)
    greeting = f"こんにちは、ノリシコです🐴\n"
    if g1_name:
        greeting += f"今日は{date_label}、{g1_name}の開催日ですね🌸"
    else:
        greeting += f"今日は{date_label}、JRA開催日です🏇"

    art = f"""📝 **{date_label}の注目データ**

{greeting}

今日のJRA全レースの中から、**過去の回収率が100%を超えているパターン**に該当する馬をピックアップしました。
血統や枠、コース適性などから統計的に"狙える傾向"があるデータをご紹介します📊

【★の見方】
★★★ 複数条件で一致・サンプル十分の強い根拠
★★　 主要条件で一致・安定傾向あり
★　　 1条件該当・参考レベル

※ 各馬の「単回XX%」は**過去の該当パターンにおける単勝回収率**、「n=XX」は**集計サンプル数**です。
※ 統計的な傾向であり、的中を保証するものではありません。ご自身の判断でお楽しみください🙏

━━━━━━━━━━━━━━━

"""
    seen_patterns = {}  # pattern_desc -> 初出馬名
    endings_used = []
    idx = 0
    for hp in day_hotspots:
        best = max(hp['matches'], key=lambda m: m['conf'] * 1000 + m['roi'])
        stars = '★' * hp['best_conf']
        pattern_key = best['desc']
        odds = hp.get('odds', 0) or 0
        if pattern_key in seen_patterns:
            # 同パターン2頭目以降: パターン名を明示して自己完結化
            first_horse = seen_patterns[pattern_key]
            art += f"▷ **{hp['venue']}{hp['race_num']}R {hp['horse_name'].strip()}**\n"
            art += f"{stars} {pattern_key}｜単回{best['roi']:.0f}%（n={best['n']}）\n"
            art += f"上述の{first_horse}と同じパターン。オッズは{odds:.1f}倍で、こちらも候補。\n\n"
        else:
            comment = _yuki_comment(hp, best, idx=idx, endings_used=endings_used)
            art += f"▷ **{hp['venue']}{hp['race_num']}R {hp['horse_name'].strip()}**\n"
            art += f"{stars} {best['desc']}｜単回{best['roi']:.0f}%（n={best['n']}）\n"
            art += f"{comment}\n\n"
            seen_patterns[pattern_key] = hp['horse_name'].strip()
        idx += 1

    art += f"""━━━━━━━━━━━━━━━

以上、{date_label}の注目データでした🐴
参考になれば嬉しいです✨

よければ♡スキで応援してもらえるとモチベになります！
フォローもお待ちしてます🙏

━━━━━━━━━━━━━━━

⚠️ 馬券は必ず自己責任でお願いします。
本記事内のオッズは投稿時点の想定値で、最終オッズとは異なる場合があります。
データは過去実績に基づく集計であり、将来の結果を保証するものではありません。

#競馬 #競馬予想 #AI予想 #データ競馬"""

    # G1ハッシュタグ
    if g1_name:
        art += f" #{g1_name}"
    return art


# 以下、既存の買い目記事ロジックは残すが未使用(上の generate_article が注目データ特化)
def _unused_buy_article(day_buys, day, date_label):
    total_inv = 0
    for p in day_buys:
        bt = p['buy_type']
        if bt == 'v6_challenge': total_inv += 1000
        elif bt: total_inv += 2000
    intro = ""
    art = f"{date_label} {total_inv}円"
    for p in day_buys:
        r = p['race']
        h = p['honmei']
        ni = p['ni']
        bt = p['buy_type']
        gap = p['gap']
        odds = h.get('odds', 0) or 0
        venue = r.get('venue', '')
        rnum = r.get('race_num', 0)
        rname = r['race_name']
        stime = r.get('start_time', '')
        grade = p['grade']
        if bt == 'v6_star3': star_emoji = '🔴'
        elif bt == 'v6_star2': star_emoji = '🟡'
        elif bt == 'v6_challenge': star_emoji = '🔵'
        else: star_emoji = '🔵'
        art += f"{star_emoji} **{venue}{rnum}R {rname}**（{stime}発走）\n\n"
        if grade == 'G1':
            art += f"G1。AIスコア差{gap:.1f}pt\n\n"
        else:
            art += f"スコア差{gap:.1f}pt\n\n"

        # 推奨馬
        art += f"◎ **{h['horse_name']}**（{h.get('jockey','')}）"
        if odds > 0:
            art += f"　想定{odds:.1f}倍"
        art += "\n"

        if ni and bt not in ('v6_challenge',):
            ni_odds = ni.get('odds', 0) or 0
            art += f"○ **{ni['horse_name']}**（{ni.get('jockey','')}）"
            if ni_odds > 0:
                art += f"　想定{ni_odds:.1f}倍"
            art += "\n"

        # 買い目（あかり案A: 高オッズは馬連寄せ）
        if bt == 'v6_challenge':
            art += f"🎯 単勝◎ 1,000円\n"
        elif bt and odds >= 8:
            art += f"🎯 単勝◎ 500円 ＋ 馬連◎○ 1,500円\n"
        elif bt:
            art += f"🎯 単勝◎ 1,000円 ＋ 馬連◎○ 1,000円\n"

        art += "\n"

    # ── 注目データセクション ──
    # 全レースの hotspot_picks を集約
    day_hotspots = []
    all_day_preds = [p for p in preds if get_day(p['race']['race_id']) == day]
    for p in all_day_preds:
        for hp in p.get('hotspot_picks', []):
            day_hotspots.append(hp)
    day_hotspots.sort(key=lambda x: (-x['best_conf'], -x['best_roi']))

    if day_hotspots:
        art += "━━━━━━━━━━━━━━━\n\n"
        art += "📊 **今日の注目データ**\n\n"
        art += "過去の回収率100%超パターンに該当する馬をAIがピックアップ！\n"
        art += "（※ 買い推奨ではなく統計データとしてご参考に）\n\n"

        shown = 0
        for hp in day_hotspots:
            if shown >= 8: break  # 最大8頭表示
            stars = '★' * hp['best_conf']
            v6tag = '（AI◎）' if hp.get('is_v6_honmei') else ''
            best_match = max(hp['matches'], key=lambda m: m['conf'] * 1000 + m['roi'])
            art += f"{stars} **{hp['venue']}{hp['race_num']}R {hp['horse_name'].strip()}**{v6tag}\n"
            art += f"　{best_match['desc']}　→ 過去単回 **{best_match['roi']:.0f}%** (n={best_match['n']})\n\n"
            shown += 1

        if len(day_hotspots) > 8:
            art += f"…ほか{len(day_hotspots) - 8}頭が該当（HTMLで全件確認できます）\n\n"

    # 締め
    art += f"""━━━━━━━━━━━━━━━

💰 今日の投資合計：**{total_inv:,}円**（{len(day_buys)}レース）

今週もAIを信じて頑張ります💪🐴
当たったら報告しますね！
応援してくれると嬉しいです✨

━━━━━━━━━━━━━━━

⚠️ あくまでAI予想です！馬券は自己責任でお願いします🙏
想定オッズは確定前のものです。

#競馬 #競馬予想 #競馬女子 #AI予想 #頑張って予想した"""

    # G1ハッシュタグ: レース名から動的に生成
    for p in day_buys:
        if p['grade'] == 'G1':
            rname = p['race'].get('race_name', '')
            art += f" #{rname}"
            break
    if any('ダービー卿' in p['race'].get('race_name','') for p in day_buys):
        art += " #ダービー卿CT"

    return art


def generate_x_post(day_buys, date_label):
    """X投稿用テキスト（100文字以内）"""
    # 目玉レースを特定
    top = day_buys[0] if day_buys else None
    if not top: return ""

    r = top['race']
    h = top['honmei']

    g1 = [p for p in day_buys if p['grade'] == 'G1']
    if g1:
        r = g1[0]['race']
        h = g1[0]['honmei']
        return f"🐴{date_label}のAI予想更新！\n{r['race_name']}◎{h['horse_name']}\n全{len(day_buys)}R厳選しました✨\n#競馬予想 #AI予想 #頑張って予想した"
    else:
        return f"🐴{date_label}のAI予想更新！\n{r['race_name']}含む{len(day_buys)}R厳選🔥\n#競馬予想 #AI予想 #頑張って予想した"


# ── 生成 ──
for day, day_buys in [('sat', sat_buys), ('sun', sun_buys)]:
    if not day_buys: continue
    date_label = get_date_label(day)

    article = generate_article(day_buys, day, date_label)
    x_post = generate_x_post(day_buys, date_label)

    outfile = f'note_article_{day}.txt'
    with open(outfile, 'w', encoding='utf-8') as f:
        f.write(article)
        f.write(f"\n\n{'='*40}\n")
        f.write(f"【X投稿用】\n{x_post}\n")

    print(f"\n{'='*50}")
    print(f"{date_label} ({len(day_buys)}R)")
    print(f"{'='*50}")
    print(article[:500] + "...\n")
    print(f"【X投稿】\n{x_post}")
    print(f"\n→ {outfile} に保存")
