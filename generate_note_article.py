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

def _yuki_comment(hp, best):
    """Yukinote風 紹介コメント (★数で文量強弱)"""
    desc_parts = best['desc'].split('×')
    sire = desc_parts[0]
    conds = desc_parts[1:] if len(desc_parts) > 1 else []
    cond_text = '×'.join(conds) if conds else ''
    roi = best['roi']
    n = best['n']
    venue = hp['venue']

    if hp['best_conf'] >= 3:
        # 3文構成(濃いめ)
        c1 = (f"{cond_text}での{sire}産駒。" if cond_text else f"{sire}産駒の一頭。")
        c2 = f"n={n}と十分なサンプルで単回{roi:.0f}%は統計的にかなり強いパターンです。"
        if roi >= 300:
            c3 = f"今日の{venue}で最も注目したいデータ。"
        elif roi >= 200:
            c3 = "オッズも手頃な範囲で、統計的な安定感があります。"
        else:
            c3 = "複数条件が揃っており、データ上の信頼度は高め。"
        return c1 + c2 + c3
    if hp['best_conf'] >= 2:
        # 2文構成
        c1 = (f"{sire}×{cond_text}、n={n}件で単回{roi:.0f}%。" if cond_text else f"{sire}でn={n}件・単回{roi:.0f}%。")
        if roi >= 300:
            c2 = "爆発力のあるパターン、要注目です。"
        elif roi >= 200:
            c2 = "安定してプラスが出てる傾向、要チェック。"
        else:
            c2 = "データ上の優位があります。"
        return c1 + c2
    # 1文構成(軽め)
    base = f"{sire}×{cond_text}" if cond_text else sire
    return f"{base}｜データ上の根拠あり、参考までに。"


def generate_article(day_buys, day, date_label):
    """1日分のnote記事を生成 (Yukinote風 注目データ紹介に特化)"""
    all_day_preds = [p for p in preds if get_day(p['race']['race_id']) == day]
    day_hotspots = []
    for p in all_day_preds:
        for hp in p.get('hotspot_picks', []):
            day_hotspots.append(hp)
    day_hotspots.sort(key=lambda x: (-x['best_conf'], -x['best_roi']))

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

※ 統計的な傾向であり、的中を保証するものではありません。ご自身の判断でお楽しみください🙏

━━━━━━━━━━━━━━━

"""
    shown = 0
    for hp in day_hotspots:
        if shown >= 10: break
        best = max(hp['matches'], key=lambda m: m['conf'] * 1000 + m['roi'])
        stars = '★' * hp['best_conf']
        comment = _yuki_comment(hp, best)
        art += f"▷ **{hp['venue']}{hp['race_num']}R {hp['horse_name'].strip()}**\n"
        art += f"{stars} {best['desc']}｜単回{best['roi']:.0f}%（n={best['n']}）\n"
        art += f"{comment}\n\n"
        shown += 1

    if len(day_hotspots) > 10:
        art += f"…ほか{len(day_hotspots) - 10}頭が該当しています📱\n\n"

    art += f"""━━━━━━━━━━━━━━━

以上、{date_label}の注目データでした🐴
参考になれば嬉しいです✨

よければ♡スキで応援してもらえるとモチベになります！
フォローもお待ちしてます🙏

━━━━━━━━━━━━━━━

⚠️ 馬券は必ず自己責任でお願いします。
想定オッズは確定前のものです。

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
