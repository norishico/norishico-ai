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

def generate_article(day_buys, day, date_label):
    """1日分のnote記事を生成"""
    total_inv = 0
    for p in day_buys:
        bt = p['buy_type']
        if bt == 'v6_challenge': total_inv += 1000
        elif bt: total_inv += 2000

    # イントロのバリエーション
    if day == 'sat':
        intro = "今週もやってきました土曜日！🐴\nお仕事終わりに予想まとめてたら日付変わってました…笑\nでも今週はAIのスコアがいい感じなので期待してます！"
    else:
        intro = "日曜日！今日は桜花賞ですね〜🌸\n朝からソワソワしてAI予想を何回も見直してしまいました笑\nさて、今週の本気予想いきます！"

    art = f"""📝 **頑張って予想した｜{date_label}**

{intro}

━━━━━━━━━━━━━━━

"""

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

        # レースヘッダー
        art += f"{star_emoji} **{venue}{rnum}R {rname}**（{stime}発走）\n\n"

        # コメント（レースごとに2-3行）
        if grade == 'G1':
            art += f"G1は慎重にいきたいけど…AIスコアが飛び抜けてるので勝負！\n"
            art += f"2位と{gap:.1f}ptも差があるのはなかなか見ない数字です👀\n\n"
        elif grade in ('G2', 'G3'):
            if odds >= 10:
                art += f"想定{odds:.1f}倍の中穴ゾーン。AIが見つけた配当妙味のある1頭🔍\n"
                art += f"スコア差{gap:.1f}ptでしっかり抜けてます！\n\n"
            else:
                art += f"AIスコアが高くて調教も良い感じ💪\n"
                art += f"重賞だけど堅実に狙えそうなレースです！\n\n"
        elif gap >= 8:
            art += f"スコア差{gap:.1f}ptで◎がかなり突出してます📊\n"
            art += f"AIが自信を持ってる1戦！\n\n"
        else:
            art += f"調教の動きが良くてAIスコアも上位🐴\n"
            art += f"堅めの予想で手堅くいきたいです！\n\n"

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
