"""weekend_predictions.jsonから予想HTMLを生成（v2: タブ切替+FB全反映）"""
import json, os, glob
from datetime import datetime
from collections import defaultdict
from alerts_log import load_alerts

preds = json.load(open('weekend_predictions.json', encoding='utf-8'))

# アラート: 蓄積ログから読み込み
alerts_data = load_alerts()
alerts = [f"<span class=\"alert-time\">{a['time']}</span> {a['text']}" for a in alerts_data]

# 月間結果（あれば読み込み）
monthly_files = sorted(glob.glob('monthly_results_*.json'))
monthly_data = json.load(open(monthly_files[-1], encoding='utf-8')) if monthly_files else None

# ── なかのひとたち: メンバー定義 ──
MEMBERS = {
    'みなみ': {'age': 26, 'role': 'データ分析', 'icon': '📊', 'color': '#2060D0'},
    'れいな': {'age': 29, 'role': '現場観察', 'icon': '👁', 'color': '#D05050'},
    'ゆきこ': {'age': 31, 'role': 'リスク管理', 'icon': '🛡', 'color': '#505090'},
    'さくら': {'age': 24, 'role': '血統・調教', 'icon': '🧬', 'color': '#D070A0'},
    'あかり': {'age': 27, 'role': 'オッズ分析', 'icon': '💹', 'color': '#30A060'},
    'ひなた': {'age': 28, 'role': '展開予想', 'icon': '🏇', 'color': '#C08020'},
    'あおい': {'age': 30, 'role': '騎手・厩舎', 'icon': '🎯', 'color': '#6060B0'},
}

def generate_member_comments(p):
    """期待値あり��ースに対してメンバーのコメントを生成"""
    r = p['race']
    h = p['honmei']
    ni = p.get('ni', {})
    gr = p['grade']
    gap = p['gap']
    odds = h.get('odds', 0) or 0
    venue = r.get('venue', '')
    rname = r.get('race_name', '')
    jockey = h.get('jockey', '')
    sire = h.get('_sire', '')
    good_train = h.get('has_good_train', False)
    accel = h.get('accel_lap', False)
    sp = p.get('special_horse')
    buy_type = p.get('buy_type', '')
    heads = p.get('heads', 0)
    ni_name = ni.get('horse_name', '') if ni else ''
    ni_jockey = ni.get('jockey', '') if ni else ''
    reasons = p.get('reasons', [])
    dist = r.get('distance', 0)
    surface = r.get('surface', '')

    comments = []
    used = set()

    # 1人目: レースの最大の特徴に合った専門家
    if good_train or accel:
        if good_train and accel:
            comments.append(('さくら', f'{h["horse_name"]}、好調教+加速ラップ！仕上がりは本物よ。'))
        else:
            comments.append(('さくら', f'{sire}産駒で調教の動きがいい。{venue}{surface}{dist}mとの相性に期待。'))
        used.add('さくら')
    elif gap >= 10:
        comments.append(('みなみ', f'gap{gap:.1f}pt、過去のバックテストだとgap10以上は的中率が高いゾーン。'))
        used.add('みなみ')
    elif odds >= 12:
        comments.append(('あかり', f'想定{odds:.1f}倍、市場が過小評価してる匂いがする。狙い目ね。'))
        used.add('あかり')
    elif sp:
        comments.append(('れいな', f'覚醒シグナル枠。スコアリングの外から穴馬を発掘、こういうの好き。'))
        used.add('れいな')
    else:
        comments.append(('みなみ', f'EV{p["ev7"]:.1f}でオッズとの乖離が大きいのがポイント。'))
        used.add('みなみ')

    # 2人目: レース条件に合った別の専門家
    if gr in ('G1', 'G2', 'G3') and 'あおい' not in used:
        top_jockeys = ('武豊','ルメール','川田','松山','横山武','戸崎','横山典')
        if jockey in top_jockeys:
            c = f'◎{jockey}は重賞実績あり、頼れる。'
            if ni_jockey: c += f'○{ni_jockey}との組み合わせもアツい。'
        else:
            c = f'◎{jockey}、重賞でどこまでやれるか。'
            if ni_jockey in top_jockeys: c += f'○{ni_jockey}のほうが実績ある分、馬連の安心感はある。'
        comments.append(('あおい', c))
        used.add('あおい')
    elif heads >= 15 and 'ひなた' not in used:
        comments.append(('ひなた', f'{heads}頭の多頭数。{surface}{dist}mは{"先行有利の流れになりやすい" if dist <= 1600 else "差しも届く展開がありえる"}。'))
        used.add('ひなた')
    elif odds >= 8 and 'あかり' not in used:
        comments.append(('あかり', f'想定{odds:.1f}倍の中穴帯。妙味と安定のバランスがいい。'))
        used.add('あかり')
    elif gap >= 5 and 'みなみ' not in used:
        comments.append(('みなみ', f'gap{gap:.1f}ptでEV{p["ev7"]:.1f}。数字的には十分に期待値あり。'))
        used.add('みなみ')

    # 3人目: チャレンジ/別枠のときだけゆきこがリスク面を補足
    if buy_type == 'v6_challenge':
        comments.append(('ゆきこ', f'チャレンジ枠だから単勝1,000円のみ。攻めすぎないのが大事。'))
    elif sp:
        comments.append(('ゆきこ', f'別枠ルールで単勝1,000円。メインとは別ポートフォリオで管理してね。'))

    return comments


def generate_graded_comments(p):
    """重賞レースに対して7人全員が1コメントずつ（座談会形式）"""
    r = p['race']
    h = p['honmei']
    ni = p.get('ni', {})
    gap = p['gap']
    odds = h.get('odds', 0) or 0
    venue = r.get('venue', '')
    rname = r.get('race_name', '')
    jockey = h.get('jockey', '')
    sire = h.get('_sire', '')
    dam_sire = h.get('_dam_sire', '')
    good_train = h.get('has_good_train', False)
    accel = h.get('accel_lap', False)
    buy_type = p.get('buy_type', '')
    heads = p.get('heads', 0)
    ni_name = ni.get('horse_name', '') if ni else ''
    ni_jockey = ni.get('jockey', '') if ni else ''
    ni_odds = (ni.get('odds', 0) or 0) if ni else 0
    dist = r.get('distance', 0)
    surface = r.get('surface', '')
    results = p.get('results', [])
    top3 = results[:3] if results else []

    comments = []

    # みなみ: 数字で語る
    if gap >= 8:
        comments.append(('みなみ', f'スコア差{gap:.1f}ptは重賞としてはかなりの突出度。◎が抜けてる根拠はEV{p["ev7"]:.1f}、統計的に狙えるゾーンよ。'))
    elif gap >= 4:
        comments.append(('みなみ', f'gap{gap:.1f}ptで接戦模様だけど、EV{p["ev7"]:.1f}でオッズとの乖離が買いの根拠。2着候補との差は僅差だから馬連の精度が問われるレース。'))
    else:
        comments.append(('みなみ', f'gap{gap:.1f}ptで上位が団子状態。荒れる可能性もあるけど、EV{p["ev7"]:.1f}で◎の期待値は確保できてる。'))

    # れいな: 現場感覚・直感
    if odds >= 10:
        comments.append(('れいな', f'想定{odds:.1f}倍で重賞の中穴狙い。こういう「みんなが見落としてる馬」が来ると配当デカいのよ。単勝だけでも美味しい。'))
    elif buy_type:
        comments.append(('れいな', f'AIが買いって言ってるなら信じる。{rname}は{venue}の{surface}{dist}m、{"荒れやすい舞台" if heads >= 14 else "実力勝負の舞台"}だから面白くなりそう。'))
    else:
        comments.append(('れいな', f'今回は買い条件には入ってないけど、◎{h["horse_name"]}の力は認めてる。オッズ次第で当日判断もアリかな。'))

    # ゆきこ: リスク管理・設計視点
    if buy_type == 'v6_challenge':
        comments.append(('ゆきこ', f'チャレンジゾーンだから単勝1,000円のみの設計。リスクを抑えつつ、当たれば{odds:.0f}倍返し。資金管理的に正しいアプローチね。'))
    elif buy_type:
        if odds >= 8:
            comments.append(('ゆきこ', f'通常ゾーン・高オッズ帯で馬連寄せ配分（単500+馬連1500）。◎{odds:.1f}倍は馬連的中時の回収が大きいから、馬連比率を上げる設計。'))
        else:
            comments.append(('ゆきこ', f'通常ゾーンで単勝+馬連の2,000円。◎{odds:.1f}倍の堅い軸なら単勝で安定回収。馬連◎○は{ni_odds:.1f}倍との組み合わせ。'))
    else:
        comments.append(('ゆきこ', f'EV条件やオッズ条件が合わず買い対象外。ルールに忠実に、ここは見送りが正解よ。'))

    # さくら: 血統・調教を熱く
    train_comment = ''
    if good_train and accel:
        train_comment = '好調教+加速ラップで仕上がりは完璧！'
    elif good_train:
        train_comment = '調教の動きは良好。'
    elif accel:
        train_comment = '加速ラップが出てるのは好材料。'

    if sire:
        blood = f'{sire}産駒'
        if dam_sire: blood += f'（母父{dam_sire}）'
        comments.append(('さくら', f'{blood}の{venue}{surface}{dist}m。{train_comment}この舞台で力を出せる血統構成だと思う！'))
    else:
        comments.append(('さくら', f'{train_comment if train_comment else "血統データは要確認。"}調教内容から見る限り、状態は{"上々" if good_train else "平凡"}ね。'))

    # あかり: オッズ・市場分析
    if results and len(results) >= 3:
        pop_1 = next((x for x in results if x.get('popularity') == 1), None)
        pop_1_name = pop_1['horse_name'] if pop_1 else '1番人気'
        if h.get('popularity', 99) <= 3:
            comments.append(('あかり', f'◎は{h.get("popularity","")}番人気で{odds:.1f}倍。人気サイドだけど、スコアが裏付けてるなら素直に買っていい。過剰人気じゃない。'))
        else:
            comments.append(('あかり', f'◎は{h.get("popularity","")}番人気で{odds:.1f}倍。市場は{pop_1_name}に集中してるけど、AIスコアは◎が上。ここに妙味がある。'))
    else:
        comments.append(('あかり', f'想定{odds:.1f}倍。最終オッズで大きく動く可能性あり。発走直前のチェックを忘れずに。'))

    # ひなた: 展開予想
    if surface == '芝' and dist <= 1600:
        comments.append(('ひなた', f'{venue}芝{dist}mは{"内回りで先行有利" if venue in ("阪神","中山") else "直線長めで差しも届く"}。{heads}頭立てで{"ペースが速くなりそう" if heads >= 14 else "落ち着いた流れになるかも"}。◎の位置取りがカギね。'))
    elif surface == '芝':
        comments.append(('ひなた', f'芝{dist}mの{"中距離戦" if dist <= 2000 else "長距離戦"}。スタミナと折り合いが問われる。{heads}頭で{"ペースが緩みやすく" if heads <= 12 else "ハイペースになる可能性も"}。展開次第で着順が大きく変わるレース。'))
    else:
        comments.append(('ひなた', f'ダ{dist}m、{venue}のダートは{"先行有利" if venue in ("中山","阪神") else "差しも決まる"}コース。砂をかぶらない外枠の先行馬に注目。'))

    # あおい: 騎手・厩舎
    top_jockeys = ('武豊','ルメール','川田','松山','横山武','戸崎','横山典','Cデムーロ')
    j_comment = ''
    if jockey in top_jockeys:
        j_comment = f'◎{jockey}は実績十分、この舞台で信頼できる。'
    else:
        j_comment = f'◎{jockey}は重賞での手腕が問われる一戦。'
    if ni_jockey:
        if ni_jockey in top_jockeys and jockey not in top_jockeys:
            j_comment += f'むしろ○{ni_jockey}の方が騎手力では上。馬連なら両方押さえられるのが◎。'
        elif ni_jockey in top_jockeys:
            j_comment += f'○{ni_jockey}も一流。この2頭の組み合わせは馬連で厚く行きたい。'
    comments.append(('あおい', j_comment))

    return comments


def waku_class(waku):
    if waku == 0: return 'waku-0'
    return f'waku-{min(waku, 8)}'

def get_date_info(race_id):
    """race_idの日目から自動で日付を計算"""
    from datetime import datetime, timedelta
    day_num = int(race_id[8:10])
    today = datetime.now()
    wd = today.weekday()
    if wd <= 4:
        sat = today + timedelta(days=(5 - wd))
    elif wd == 5:
        sat = today
    else:
        sat = today - timedelta(days=1)
    # 奇数日目=土曜、偶数日目=日曜
    dt = sat if day_num % 2 == 1 else sat + timedelta(days=1)
    weekdays = ['月', '火', '水', '木', '金', '土', '日']
    wd_str = weekdays[dt.weekday()]
    short = f"{dt.month}/{dt.day}({wd_str})"
    key = dt.strftime('%m%d')
    label = f"{dt.year}年{dt.month}月{dt.day}日（{wd_str}）"
    return short, key, label

for p in preds:
    p['_date_short'], p['_date_key'], p['_date_label'] = get_date_info(p['race']['race_id'])

preds.sort(key=lambda p: (p['_date_key'], p['race'].get('venue',''), p['race'].get('race_num',0)))

buy_preds = [p for p in preds if p['buy_type'] or p['special_horse']]
def _calc_inv(p):
    bt = p.get('buy_type', '')
    ho = p.get('honmei', {}).get('odds', 0) or 0
    gap = p.get('gap', 0) or 0
    base = 0
    if bt == 'v6_challenge': base = 1000
    elif bt: base = 2000
    elif p.get('special_horse'): base = 1000
    # 3連単フォーメーション追加 (gap5+ & odds8+)
    if bt and gap >= 5 and ho >= 8: base += 2400
    return 0
total_inv = sum(_calc_inv(p) for p in buy_preds)

# 日付・会場の一覧
all_dates = sorted(set(p['_date_key'] for p in preds))
all_venues = sorted(set(p['race'].get('venue','') for p in preds))
date_labels = {p['_date_key']: p['_date_label'] for p in preds}
date_shorts = {p['_date_key']: p['_date_short'] for p in preds}

def race_card_html(p, show_full=True):
    """1レースのカードHTML"""
    r = p['race']
    rnum = r.get('race_num', 0)
    rname = r.get('race_name', '')
    stime = r.get('start_time', '')
    surf = r.get('surface', '')
    dist = r.get('distance', '')
    heads = p['heads']
    honmei = p['honmei']
    ni = p['ni']
    buy = p['buy_type']
    sp = p['special_horse']
    venue = r.get('venue', '')
    is_buy = bool(buy or sp)

    if buy == 'v6_star3': stars='★★★'; conf='自信の一戦'; type_label='AI本命予想'
    elif buy == 'v6_star2': stars='★★'; conf='注目レース'; type_label='AI本命予想'
    elif buy == 'v6_challenge': stars='★'; conf='チャレンジ枠'; type_label='AI穴狙い'
    elif sp:
        stars='★'; conf='チャレンジ枠'
        type_label = '新馬スカウト' if 'C2' in sp.get('rule','') else '覚醒シグナル'
    else: stars=''; conf=''; type_label=''

    if buy == 'v6_star3': card_cls = 'race-card star3'
    elif buy == 'v6_star2': card_cls = 'race-card star2'
    elif buy == 'v6_challenge': card_cls = 'race-card star1'
    elif sp: card_cls = 'race-card star1'
    elif is_buy: card_cls = 'race-card star2'
    else: card_cls = 'race-card nobuy'
    num_cls = 'race-num buy-num' if is_buy else 'race-num'

    h = ''
    h += f'<div class="{card_cls}" data-date="{p["_date_key"]}" data-venue="{venue}">\n'

    # タイプラベル（期待値ありのみ）
    if type_label:
        h += f'  <div class="type-label">{type_label}</div>\n'

    h += f'  <div class="race-header">\n'
    h += f'    <div class="race-info"><span class="{num_cls}">{venue}{rnum}R</span><div>'
    h += f'<div class="race-name">{rname}</div>'
    h += f'<div class="race-detail">{surf}{dist}m {heads}頭</div>'
    h += f'<div class="race-time">発走 {stime}</div></div></div>\n'
    if stars:
        h += f'    <div class="confidence"><div class="stars">{stars}</div><div class="conf-label">{conf}</div></div>\n'
    h += f'  </div>\n'

    if is_buy:
        h += '  <div class="picks-and-bet">\n'
        if buy:
            wc_h = waku_class(honmei.get('waku',0))
            hn_h = honmei.get('horse_num','-')
            h += f'    <div class="pick-row"><div class="mark honmei">◎</div>'
            h += f'<span class="umaban {wc_h}">{hn_h}</span>'
            h += f'<div class="horse-info"><span class="horse-name">{honmei["horse_name"]}</span>'
            h += f'<span class="jockey-name">{honmei.get("jockey","")}</span></div>'
            h += f'<div class="pick-right"><div class="score-value">{honmei["total_score"]:.1f}</div><div class="score-sub">/100</div></div></div>\n'
            if ni:
                wc_n = waku_class(ni.get('waku',0))
                hn_n = ni.get('horse_num','-')
                h += f'    <div class="pick-row"><div class="mark ni">○</div>'
                h += f'<span class="umaban {wc_n}">{hn_n}</span>'
                h += f'<div class="horse-info"><span class="horse-name">{ni["horse_name"]}</span>'
                h += f'<span class="jockey-name">{ni.get("jockey","")}</span></div>'
                h += f'<div class="pick-right"><div class="score-value">{ni["total_score"]:.1f}</div><div class="score-sub">/100</div></div></div>\n'
            h += '    <div class="bet-inline"><div class="bet-chips">'
            ho = honmei.get('odds', 0) or 0
            if buy == 'v6_challenge':
                h += '<div class="bet-chip"><span class="type">単勝◎</span> <span class="amount">1,000円</span></div>'
                h += '<span class="bet-total-inline">計 1,000円</span></div></div>\n'
            elif ho >= 8:
                # あかり案A: 高オッズ帯は馬連寄せ
                h += '<div class="bet-chip"><span class="type">単勝◎</span> <span class="amount">500円</span></div>'
                h += '<div class="bet-chip"><span class="type">馬連◎○</span> <span class="amount">1,500円</span></div>'
                h += '<span class="bet-total-inline">計 2,000円</span></div></div>\n'
            else:
                h += '<div class="bet-chip"><span class="type">単勝◎</span> <span class="amount">1,000円</span></div>'
                h += '<div class="bet-chip"><span class="type">馬連◎○</span> <span class="amount">1,000円</span></div>'
                h += '<span class="bet-total-inline">計 2,000円</span></div></div>\n'
        # 3連単フォーメーション表示 (gap5+ & odds8+)
        san_targets = p.get('sanrentan_targets')
        if san_targets:
            h_name = honmei.get('horse_name','').strip()
            h_num = honmei.get('horse_num', '?')
            n_name = ni.get('horse_name','').strip() if ni else ''
            n_num = ni.get('horse_num', '?') if ni else '?'
            t_names = ', '.join(f'{t["name"].strip()}' for t in san_targets)
            t_nums = ', '.join(str(t.get('horse_num','?')) for t in san_targets)
            all_2nd = f'{h_num} {n_num} {t_nums}'
            h += '    <div class="bet-3rentan">\n'
            h += '      <div class="san-header">3連単フォーメーション 24点×100円＝2,400円</div>\n'
            h += f'      <div class="san-row"><span class="san-pos">1着</span><span class="san-arrow">→</span><span class="san-horses">◎{h_name}、○{n_name}</span></div>\n'
            h += f'      <div class="san-row"><span class="san-pos">2着</span><span class="san-arrow">→</span><span class="san-horses">◎○＋ {t_names}</span></div>\n'
            h += f'      <div class="san-row"><span class="san-pos">3着</span><span class="san-arrow">→</span><span class="san-horses">◎○＋ {t_names}</span></div>\n'
            h += f'      <div class="san-nums">馬番: 1着[{h_num},{n_num}] 2着[{all_2nd}] 3着[{all_2nd}]</div>\n'
            h += '    </div>\n'

        if sp:
            rule = sp.get('rule','')
            sp_waku = sp.get('waku', 0)
            sp_umaban = sp.get('horse_num', '-')
            sp_wc = waku_class(sp_waku)
            h += f'    <div class="pick-row"><div class="mark special">◆</div>'
            h += f'<span class="umaban {sp_wc}">{sp_umaban}</span>'
            h += f'<div class="horse-info"><span class="horse-name">{sp["horse_name"]}</span>'
            h += f'<span class="jockey-name">{sp.get("jockey","")}</span></div>'
            h += f'<div class="pick-right"><div class="score-value" style="font-size:12px;color:var(--green)">{type_label}</div></div></div>\n'
            h += '    <div class="bet-inline"><div class="bet-chips">'
            h += '<div class="bet-chip"><span class="type">単勝</span> <span class="amount">1,000円</span></div>'
            h += '<span class="bet-total-inline">計 1,000円</span></div></div>\n'
        h += '  </div>\n'

        # 根拠タグ（優先度順に最大2つ + 残りは折りたたみ）
        priority_tags = []  # 特別バッジ（色付き）
        normal_tags = []    # 通常バッジ
        for tag in p['reasons']:
            if tag in ('楽逃げ候補', '前走不利僅差惜敗', '前走不利克服') or tag.startswith('初ダート') or tag.startswith('初芝'):
                priority_tags.append(tag)
            else:
                normal_tags.append(tag)
        # 逃げ候補数もバッジとして統合
        nc = p.get('nige_candidates', 0)
        if nc > 0 and '楽逃げ候補' not in priority_tags:
            normal_tags.append(f'逃げ候補{nc}頭')

        all_tags = priority_tags + normal_tags
        show_tags = all_tags[:3]  # 常時表示は3つまで
        extra_tags = all_tags[3:]

        def _tag_cls(tag):
            if tag == '前走不利克服': return ' bias-overcome'
            if tag == '前走不利僅差惜敗': return ' bias-close-loss'
            if tag == '楽逃げ候補': return ' raku-nige'
            if tag.startswith('逃げ候補'): return ' nige-count'
            if tag.startswith('初ダート') or tag.startswith('初芝'): return ' surface-switch'
            return ''

        h += '  <div class="reason-section">'
        for tag in show_tags:
            h += f'<span class="reason-tag{_tag_cls(tag)}">{tag}</span>'
        if extra_tags:
            h += f'<span class="reason-more" onclick="this.nextElementSibling.style.display=\'inline\';this.style.display=\'none\'">+{len(extra_tags)}</span>'
            h += '<span class="reason-extra" style="display:none">'
            for tag in extra_tags:
                h += f'<span class="reason-tag{_tag_cls(tag)}">{tag}</span>'
            h += '</span>'
        h += '</div>\n'

        # オッズ
        h += '  <div class="odds-section"><div class="odds-display">'
        if buy:
            ho = honmei.get('odds',0) or 0
            no = ni.get('odds',0) if ni else 0
            h += f'<div class="odds-item"><span class="mark-sm">◎</span> <span class="odds-val">{f"{ho:.1f}" if ho > 0 else "--"}</span>{"倍" if ho > 0 else ""}</div>'
            if ni and no > 0:
                h += f'<div class="odds-item"><span class="mark-sm">○</span> <span class="odds-val">{no:.1f}</span>倍</div>'
        if sp:
            so = sp.get('odds',0) or 0
            h += f'<div class="odds-item"><span class="mark-sm">◆</span> <span class="odds-val">{f"{so:.1f}" if so > 0 else "--"}</span>{"倍" if so > 0 else ""}</div>'
        # 損益分岐オッズ表示
        be = p.get('breakeven_odds', 0)
        ho_now = honmei.get('odds', 0) or 0
        is_challenge = buy == 'v6_challenge'
        if be > 0 and ho_now > 0 and buy:
            margin_ratio = ho_now / be
            if margin_ratio >= 1.5:
                be_cls = 'be-good'
                be_text = f'分岐{be:.1f}倍 ✅ ゆとり大'
            elif margin_ratio >= 1.0:
                be_cls = 'be-ok'
                be_text = f'分岐{be:.1f}倍 ✅ オッズ十分'
            elif is_challenge:
                be_cls = 'be-warn'
                be_text = f'分岐{be:.1f}倍 ⚠ 期待値ギリギリ'
            else:
                be_cls = 'be-warn'
                be_text = f'分岐{be:.1f}倍 ⚠ 馬連込みで勝負'
            h += f'<span class="breakeven {be_cls}">{be_text}</span>'
        h += '</div><span class="odds-status pending">オッズ判定中</span></div>\n'

    # スコアバー（全レース共通）
    all_horses = p.get('results', [])
    if all_horses and show_full:
        max_s = max(x['total_score'] for x in all_horses)
        min_s = min(x['total_score'] for x in all_horses)
        rng = max_s - min_s if max_s > min_s else 1

        def sb_row_html(horse, rank):
            sc = horse['total_score']
            bar_pct = max(8, ((sc - min_s) / rng) * 90 + 10)
            if rank == 1: mc='m1'; mt='◎'; bc='b1'
            elif rank == 2: mc='m2'; mt='○'; bc='b2'
            elif rank == 3: mc='m3'; mt='▲'; bc='b3'
            else: mc=''; mt=''; bc='bn'
            wc = waku_class(horse.get('waku',0))
            hn = horse.get('horse_num', rank)
            jk = horse.get('jockey','')
            odds = horse.get('odds',0) or 0
            odds_str = f'{odds:.1f}倍' if odds > 0 else ''
            r = f'<div class="sb-row">'
            r += f'<span class="sb-rank">{rank}</span>'
            r += f'<span class="sb-mark {mc}">{mt}</span>'
            r += f'<span class="sb-umaban {wc}">{hn}</span>'
            r += f'<div class="sb-main">'
            r += f'<div class="sb-name-row"><span class="sb-name">{horse["horse_name"]}</span>'
            r += f'<span class="sb-jockey">{jk}</span>'
            if odds_str: r += f'<span class="sb-odds">{odds_str}</span>'
            r += f'</div>'
            r += f'<div class="sb-bar-wrap"><div class="sb-bar {bc}" style="width:{bar_pct:.0f}%"></div></div>'
            r += f'</div>'
            r += f'<span class="sb-score">{sc:.1f}</span></div>\n'
            return r

        # 上位3頭プレビュー
        h += '  <div class="sb-preview">\n'
        for idx in range(min(3, len(all_horses))):
            h += '    ' + sb_row_html(all_horses[idx], idx+1)
        h += '  </div>\n'

        # 4位以下は折りたたみ
        if len(all_horses) > 3:
            h += '  <button class="sb-more-toggle" onclick="this.classList.toggle(\'open\');this.nextElementSibling.classList.toggle(\'open\')">'
            h += f'4位以下を見る（残り{len(all_horses)-3}頭）<span class="arrow">▼</span></button>\n'
            h += '  <div class="sb-rest">\n'
            for idx in range(3, len(all_horses)):
                h += '    ' + sb_row_html(all_horses[idx], idx+1)
            h += '  </div>\n'

    h += '</div>\n'
    return h

# ===== HTML生成 =====
html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NORISHICO AI - 今週末の予想</title>
<link href="https://fonts.googleapis.com/css2?family=Zen+Maru+Gothic:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {{
  --orange:#D57B0E;--orange-light:#E8A040;--orange-pale:#F5C882;
  --cream:#F4E2D0;--cream-light:#FBF3EB;
  --green:#3A5633;--green-light:#5A7A50;--green-pale:#8AAA7E;
  --bg:#FBF3EB;--card-bg:#FFFFFF;--card-border:#E8D5C0;
  --text:#3A3028;--text-sub:#8A7A6A;--lose-color:#C05050;
}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Zen Maru Gothic','Hiragino Kaku Gothic ProN',sans-serif;background:var(--bg);color:var(--text);line-height:1.7;min-height:100vh}}
.waku-0{{background:#DDD;color:#333;border:2px solid #BBB}}
.waku-1{{background:#FFF;color:#333;border:2px solid #CCC}}
.waku-2{{background:#222;color:#FFF}}.waku-3{{background:#E03030;color:#FFF}}
.waku-4{{background:#2060D0;color:#FFF}}.waku-5{{background:#F0D020;color:#333}}
.waku-6{{background:#30A030;color:#FFF}}.waku-7{{background:#E07020;color:#FFF}}
.waku-8{{background:#E870A0;color:#FFF}}

/* ヘッダー（sticky） */
.sticky-header{{position:sticky;top:0;z-index:100;background:linear-gradient(135deg,var(--green),#2A4023);padding:10px 16px;display:flex;justify-content:space-between;align-items:center;border-bottom:3px solid var(--orange);box-shadow:0 2px 12px rgba(0,0,0,0.2)}}
.sticky-header .logo{{font-size:16px;font-weight:700;color:var(--cream);letter-spacing:2px}}
.sticky-header .logo span{{color:var(--orange-light)}}
.sticky-header .sub{{font-size:11px;color:var(--green-pale);text-align:right}}
.sticky-header .sub-date{{font-size:12px;color:var(--cream);font-weight:700}}

/* サマリー */
.summary{{margin:16px;padding:16px;background:linear-gradient(135deg,var(--green),#4A6A40);border-radius:12px;box-shadow:0 4px 12px rgba(58,86,51,0.2)}}
.summary-title{{font-size:12px;color:var(--orange-pale);font-weight:700;margin-bottom:10px;letter-spacing:2px}}
.summary-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}
.summary-item{{text-align:center;padding:10px 8px;background:rgba(255,255,255,0.1);border-radius:10px}}
.summary-item .label{{font-size:11px;color:var(--green-pale)}}
.summary-item .value{{font-size:20px;font-weight:700;color:#fff;margin-top:2px}}
.summary-day-split{{display:flex;justify-content:center;gap:16px;margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.1)}}
.summary-day{{display:flex;align-items:center;gap:6px}}
.day-label{{font-size:12px;color:var(--text-sub)}}
.day-picks{{font-size:16px;font-weight:700;color:var(--orange-pale)}}

/* タブ（sticky） */
.tab-bar{{display:flex;gap:6px;padding:10px 16px;overflow-x:auto;background:var(--cream);border-bottom:2px solid var(--card-border);position:sticky;z-index:90;-webkit-overflow-scrolling:touch}}
.tab-bar.sub-tabs{{background:var(--bg);border-bottom:1px solid var(--card-border)}}
.tab{{padding:12px 20px;border-radius:24px;font-size:13px;font-weight:700;cursor:pointer;background:var(--card-bg);border:2px solid var(--card-border);color:var(--text-sub);transition:all 0.2s;white-space:nowrap;min-height:44px;display:flex;align-items:center}}
.tab.active{{background:var(--orange);border-color:var(--orange);color:white}}
.sub-tabs .tab{{font-size:12px;padding:10px 16px;min-height:40px}}
.sub-tabs .tab.active{{background:var(--green);border-color:var(--green)}}
.tab-content{{display:none;opacity:0;transition:opacity 0.2s}}
.tab-content.active{{display:block;opacity:1}}
.sub-content{{display:none;opacity:0;transition:opacity 0.2s}}
.sub-content.active{{display:block;opacity:1}}

/* セクションタイトル */
.section-title{{padding:10px 16px;font-size:14px;font-weight:700;color:var(--cream);background:linear-gradient(135deg,var(--green),var(--green-light));border-radius:8px;margin:16px 16px 8px;letter-spacing:1px}}

/* タイプラベル */
.type-label{{padding:4px 16px;font-size:11px;font-weight:700;color:white;background:var(--orange);letter-spacing:1px}}

/* ── レースカード ── */
.race-card{{margin:16px;background:var(--card-bg);border:2px solid var(--card-border);border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.06)}}
/* ★★★: 大きめカード */
.race-card.star3{{border-color:var(--orange);border-left:6px solid var(--orange);box-shadow:0 6px 20px rgba(213,123,14,0.2)}}
.race-card.star3 .race-header{{padding:16px}}
.race-card.star3 .race-name{{font-size:17px}}
.race-card.star3 .horse-name{{font-size:17px}}
.race-card.star3 .score-value{{font-size:22px}}
.race-card.star3 .stars{{font-size:22px}}
/* ★★: 通常カード */
.race-card.star2{{border-color:var(--orange);border-left:5px solid var(--orange);box-shadow:0 4px 16px rgba(213,123,14,0.15)}}
/* ★: コンパクトカード */
.race-card.star1{{border-color:var(--orange-light);border-left:4px solid var(--orange-light)}}
.race-card.star1 .race-header{{padding:10px 16px}}
.race-card.star1 .horse-name{{font-size:14px}}
.race-card.star1 .score-value{{font-size:16px}}
/* 非買いカード */
.race-card.nobuy{{opacity:0.85}}

.race-header{{padding:12px 16px;display:flex;justify-content:space-between;align-items:center;background:linear-gradient(135deg,var(--cream),#FFF);border-bottom:1px solid var(--card-border)}}
.race-info{{display:flex;align-items:center;gap:10px}}
.race-num{{background:var(--green);color:white;font-size:13px;font-weight:700;padding:3px 12px;border-radius:6px}}
.race-num.buy-num{{background:var(--orange)}}
.race-name{{font-size:14px;font-weight:700}}.race-detail{{font-size:12px;color:var(--text-sub)}}
.race-time{{font-size:11px;color:var(--orange);font-weight:700;margin-top:2px}}
.confidence{{text-align:right}}
.stars{{color:var(--orange);font-size:18px;letter-spacing:2px}}
.conf-label{{font-size:11px;color:var(--text-sub);margin-top:2px}}

.picks-and-bet{{padding:14px 16px}}
.pick-row{{display:flex;align-items:center;gap:8px;padding:8px 0}}
.pick-row+.pick-row{{border-top:1px dashed var(--card-border)}}
.mark{{font-size:20px;font-weight:900;width:26px;text-align:center;flex-shrink:0}}
.mark.honmei{{color:var(--orange)}}.mark.ni{{color:var(--green)}}.mark.special{{color:var(--orange)}}
.umaban{{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:50%;font-size:12px;font-weight:700;flex-shrink:0}}
.horse-info{{flex:1;min-width:0}}
.horse-name{{font-size:15px;font-weight:700}}
.jockey-name{{font-size:11px;color:var(--text-sub);display:block;margin-top:1px}}
.pick-right{{text-align:right;flex-shrink:0}}
.score-value{{font-size:18px;font-weight:700;color:var(--orange)}}
.score-sub{{font-size:10px;color:var(--text-sub)}}
.bet-inline{{margin-top:10px;padding:10px 12px;background:var(--cream-light);border-radius:10px;border:1px solid var(--card-border)}}
.bet-3rentan{{background:linear-gradient(135deg,#FFF3E0,#FFE0B2);border:1px solid #FFB74D;border-radius:10px;margin:8px 16px;padding:12px 14px}}
.san-header{{font-size:12px;font-weight:700;color:#E65100;margin-bottom:8px}}
.san-row{{display:flex;align-items:center;gap:6px;font-size:12px;padding:2px 0}}
.san-pos{{font-weight:700;color:#E65100;min-width:28px}}
.san-arrow{{color:#FFB74D}}
.san-horses{{color:var(--text)}}
.san-nums{{font-size:10px;color:var(--text-sub);margin-top:6px;padding-top:6px;border-top:1px dashed #FFB74D}}
.bet-chips{{display:flex;flex-wrap:wrap;gap:6px;align-items:center}}
.bet-chip{{background:white;border:1px solid var(--card-border);padding:4px 12px;border-radius:8px;font-size:12px}}
.bet-chip .type{{color:var(--text-sub)}}.bet-chip .amount{{color:var(--text);font-weight:700}}
.bet-total-inline{{margin-left:auto;font-size:14px;font-weight:700;color:var(--orange)}}
.reason-section{{padding:4px 16px 12px;display:flex;flex-wrap:wrap;gap:6px}}
.reason-tag{{background:var(--cream);border:1px solid var(--card-border);padding:4px 12px;border-radius:12px;font-size:11px;color:var(--green);font-weight:500}}
.reason-tag.bias-overcome{{background:linear-gradient(135deg,#FF6B35,#F7931E);color:white;border:none;font-weight:700}}
.reason-tag.bias-close-loss{{background:linear-gradient(135deg,#E91E63,#FF5722);color:white;border:none;font-weight:700}}
.reason-tag.raku-nige{{background:linear-gradient(135deg,#2196F3,#00BCD4);color:white;border:none;font-weight:700}}
.reason-tag.nige-count{{background:#E3F2FD;color:#1565C0;border:1px solid #BBDEFB}}
.reason-more{{background:var(--cream);border:1px dashed var(--card-border);padding:4px 10px;border-radius:12px;font-size:11px;color:var(--text-sub);cursor:pointer}}
.breakeven{{font-size:10px;padding:2px 8px;border-radius:8px;margin-left:8px;font-weight:600}}
.be-good{{background:#E8F5E9;color:#2E7D32}}
.be-ok{{background:#E8F5E9;color:#558B2F}}
.be-warn{{background:#FFF3E0;color:#E65100}}
.reason-extra{{display:inline;gap:6px}}
.odds-section{{padding:12px 16px;border-top:1px solid var(--card-border);display:flex;justify-content:space-between;align-items:center}}
.odds-display{{display:flex;gap:20px}}
.odds-item{{font-size:13px;color:var(--text-sub)}}
.odds-item .mark-sm{{font-weight:700;color:var(--text)}}
.odds-item .odds-val{{font-size:18px;font-weight:700;color:var(--orange)}}
.odds-status{{font-size:12px;padding:5px 14px;border-radius:12px;font-weight:700;background:#FFF3E0;color:var(--orange);border:1px solid var(--card-border)}}

/* スコアバー: 上位3頭表示+展開 */
.sb-preview{{padding:8px 16px;border-top:1px solid var(--card-border)}}
.sb-more-toggle{{width:100%;padding:6px 16px;background:none;border:none;border-top:1px solid var(--card-border);font-family:inherit;font-size:11px;font-weight:700;color:var(--text-sub);cursor:pointer;display:flex;justify-content:space-between;align-items:center}}
.sb-more-toggle:hover{{background:var(--cream-light)}}
.sb-more-toggle .arrow{{font-size:10px;transition:transform 0.3s}}
.sb-more-toggle.open .arrow{{transform:rotate(180deg)}}
.sb-rest{{display:none;padding:0 16px 8px}}
.sb-rest.open{{display:block}}
.sb-row{{display:grid;grid-template-columns:16px 18px 22px 1fr 32px;align-items:center;gap:4px;padding:5px 0}}
.sb-rank{{text-align:right;color:var(--text-sub);font-size:11px}}
.sb-mark{{font-weight:900;font-size:14px;text-align:center}}
.sb-mark.m1{{color:var(--orange)}}.sb-mark.m2{{color:var(--green)}}.sb-mark.m3{{color:#2060D0}}
.sb-umaban{{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:50%;font-size:10px;font-weight:700}}
.sb-main{{display:flex;flex-direction:column;gap:2px;min-width:0}}
.sb-name-row{{display:flex;align-items:baseline;gap:4px;flex-wrap:wrap}}
.sb-name{{font-weight:700;font-size:12px}}
.sb-jockey{{font-size:10px;color:var(--text-sub)}}
.sb-odds{{font-size:10px;color:var(--orange)}}
.sb-bar-wrap{{height:16px;background:#EBE4DA;border-radius:8px;overflow:hidden;width:100%}}
.sb-bar{{height:100%;border-radius:8px;min-width:8px}}
.sb-bar.b1{{background:linear-gradient(90deg,var(--orange),var(--orange-light))}}
.sb-bar.b2{{background:linear-gradient(90deg,var(--green),var(--green-light))}}
.sb-bar.b3{{background:linear-gradient(90deg,#2060D0,#4080E0)}}
.sb-bar.bn{{background:#C8BEB0}}
.sb-score{{text-align:right;font-weight:700;font-size:12px;color:var(--text)}}

/* 軸馬 */
.jiku-section{{margin:16px;background:linear-gradient(135deg,#F0F7ED,#F5FAF2);border-radius:12px;padding:2px}}
.jiku-toggle{{width:100%;padding:12px 16px;background:none;border:2px solid var(--green-pale);border-radius:12px;font-family:inherit;font-size:14px;font-weight:700;color:var(--green);cursor:pointer;display:flex;justify-content:space-between;align-items:center;min-height:44px}}
.jiku-toggle .arrow{{font-size:12px;color:var(--text-sub);transition:transform 0.3s}}
.jiku-toggle.open .arrow{{transform:rotate(180deg)}}
.jiku-list{{display:none;margin-top:8px;background:var(--card-bg);border:2px solid var(--card-border);border-radius:12px;overflow:hidden}}
.jiku-list.open{{display:block}}
.jiku-row{{display:flex;align-items:center;gap:6px;padding:8px 12px;border-bottom:1px solid #F0E8E0;font-size:12px}}
.jiku-row:last-child{{border-bottom:none}}
.jiku-rnum{{background:var(--text-sub);color:white;font-size:10px;font-weight:700;padding:2px 6px;border-radius:4px;flex-shrink:0}}
.jiku-rnum.has-bet{{background:var(--orange)}}
.jiku-course{{color:var(--text-sub);font-size:11px;flex-shrink:0;width:68px}}
.jiku-mark{{color:var(--orange);font-weight:900;font-size:14px;flex-shrink:0}}
.jiku-horse{{font-weight:700;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.jiku-jockey{{color:var(--text-sub);font-size:11px;flex-shrink:0}}
.jiku-score{{font-weight:700;color:var(--orange);font-size:13px;flex-shrink:0;width:32px;text-align:right}}
.jiku-row.bought{{background:var(--cream-light)}}
.jiku-tag{{font-size:9px;background:var(--orange);color:white;padding:1px 6px;border-radius:4px;font-weight:700;margin-left:2px}}

/* アラート */
.alert-banner{{margin:12px 16px;padding:10px 14px;background:#FFF3E0;border:2px solid var(--orange-light);border-radius:10px;font-size:12px;line-height:1.8;color:var(--text)}}
.alert-banner .alert-title{{font-size:11px;font-weight:700;color:var(--orange);margin-bottom:4px;letter-spacing:1px}}
.alert-banner .alert-item{{padding:2px 0}}
.alert-banner .alert-time{{font-size:10px;color:var(--text-sub);font-weight:700;margin-right:4px}}

/* 結果タブ */
.result-summary{{margin:16px;padding:16px;background:linear-gradient(135deg,var(--green),#4A6A40);border-radius:12px;color:white;box-shadow:0 4px 12px rgba(58,86,51,0.2)}}
.result-summary .rs-title{{font-size:11px;color:var(--orange-pale);font-weight:700;letter-spacing:2px;margin-bottom:10px}}
.result-summary .rs-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}
.result-summary .rs-item{{text-align:center;padding:10px 8px;background:rgba(255,255,255,0.1);border-radius:10px}}
.result-summary .rs-label{{font-size:10px;color:var(--green-pale)}}
.result-summary .rs-value{{font-size:20px;font-weight:700;margin-top:2px;color:#fff}}
.result-summary .rs-plus{{color:var(--orange-pale)}}.result-summary .rs-minus{{color:#F0A0A0}}.result-summary .rs-zero{{color:var(--green-pale)}}
.result-day{{margin:16px;background:var(--card-bg);border:2px solid var(--card-border);border-radius:12px;overflow:hidden}}
.result-day-header{{padding:10px 16px;background:linear-gradient(135deg,var(--cream),#FFF);border-bottom:1px solid var(--card-border);display:flex;justify-content:space-between;align-items:center}}
.result-day-header .day-label{{font-weight:700;font-size:13px}}
.result-day-header .cond-badges{{display:flex;gap:4px}}
.result-day-header .cond-badge{{font-size:10px;padding:2px 8px;border-radius:8px;background:var(--cream);border:1px solid var(--card-border);color:var(--text-sub)}}
.result-race{{display:flex;align-items:center;gap:8px;padding:10px 16px;border-bottom:1px solid #F0E8E0}}
.result-race:last-child{{border-bottom:none}}
.result-race .rr-badge{{background:var(--orange);color:white;font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;flex-shrink:0}}
.result-race .rr-body{{flex:1;min-width:0}}
.result-race .rr-title{{font-size:12px;font-weight:700}}
.result-race .rr-detail{{font-size:11px;color:var(--text-sub);margin-top:2px}}
.result-race .rr-pnl{{font-size:14px;font-weight:700;flex-shrink:0}}
.rr-pnl.win{{color:#50C878}}.rr-pnl.lose{{color:#E06060}}

/* なかのひとたち */
/* 注目データ */
.hotspot-venue-group{{margin-bottom:12px}}
.hotspot-venue-label{{padding:8px 16px 4px;font-size:13px;font-weight:700;color:var(--green-pale);border-bottom:1px solid rgba(255,255,255,0.08);margin:0 16px 4px}}
.hotspot-card{{margin:8px 16px;padding:10px 14px;background:var(--card-bg);border:1px solid var(--card-border);border-radius:10px}}
.hs-header{{display:flex;align-items:center;gap:8px;margin-bottom:6px}}
.hs-race{{font-weight:700;font-size:12px;color:var(--green)}}
.hs-horse{{font-weight:700;font-size:13px;flex:1}}
.hs-odds{{font-size:12px;color:var(--text-sub)}}
.hs-v6{{background:var(--orange);color:white;font-size:9px;padding:2px 6px;border-radius:4px;font-weight:700}}
.hs-v6buy{{background:var(--green);color:white;font-size:9px;padding:2px 6px;border-radius:4px;font-weight:700}}
.hs-match{{display:flex;align-items:center;gap:6px;padding:3px 0;font-size:11px}}
.hs-stars{{color:#F59E0B;font-weight:700;min-width:30px}}
.hs-desc{{flex:1;color:var(--text)}}
.hs-roi{{font-weight:700;color:var(--orange)}}
.hs-n{{color:var(--text-sub);font-size:10px}}
.hs-conf3 .hs-roi{{color:#D32F2F}}
.hs-conf2 .hs-roi{{color:var(--orange)}}
.nakanohito-section{{margin:16px;padding:0}}
.nakanohito-race{{margin-bottom:20px;background:var(--card-bg);border:2px solid var(--card-border);border-radius:12px;overflow:hidden}}
.nakanohito-race-header{{padding:10px 16px;background:linear-gradient(135deg,var(--cream),#FFF);border-bottom:1px solid var(--card-border);font-weight:700;font-size:13px;display:flex;align-items:center;gap:8px}}
.nakanohito-race-header .race-badge{{background:var(--orange);color:white;padding:2px 10px;border-radius:6px;font-size:11px}}
.comment-list{{padding:8px 0}}
.comment-row{{display:flex;gap:10px;padding:10px 16px;border-bottom:1px solid #F5EDE5}}
.comment-row:last-child{{border-bottom:none}}
.comment-avatar{{flex-shrink:0;width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:18px;border:2px solid #E8D5C0}}
.comment-body{{flex:1;min-width:0}}
.comment-name{{font-size:12px;font-weight:700;display:flex;align-items:center;gap:6px}}
.comment-role{{font-size:10px;color:var(--text-sub);font-weight:400;background:var(--cream);padding:1px 8px;border-radius:8px}}
.comment-text{{font-size:13px;line-height:1.6;margin-top:3px;color:var(--text)}}

/* フッター */
.footer{{margin:24px 0 0;padding:20px 16px;text-align:center;font-size:11px;color:var(--green-pale);background:linear-gradient(135deg,var(--green),#2A4023);border-top:3px solid var(--orange)}}
.footer a{{color:var(--orange-pale)}}
.footer .disclaimer{{margin-top:10px;padding:10px;background:rgba(255,255,255,0.1);border-radius:8px;color:var(--green-pale)}}
@media(min-width:768px){{body{{max-width:640px;margin:0 auto}}}}
</style>
</head>
<body>

<div class="sticky-header">
  <div class="logo"><span>NORISHICO</span> AI</div>
  <div class="sub">
    <div class="sub-date">{' / '.join(date_shorts[dk] for dk in all_dates)}</div>
    {'・'.join(all_venues)}
  </div>
</div>

<div class="summary">
  <div class="summary-title">WEEKEND PICKS</div>
  <div class="summary-grid">
    <div class="summary-item"><div class="label">厳選レース</div><div class="value">{len(buy_preds)}R</div></div>
    <div class="summary-item"><div class="label">全レース</div><div class="value">{len(preds)}R</div></div>
    <div class="summary-item"><div class="label">想定投資</div><div class="value">{total_inv:,}円</div></div>
  </div>
  <div class="summary-day-split">
    {''.join(f'<div class="summary-day"><span class="day-label">{date_shorts[dk]}</span><span class="day-picks">{len([p for p in buy_preds if p["_date_key"]==dk])}R</span></div>' for dk in all_dates)}
  </div>
</div>

"""

# 日付タブ + 結果タブ
html += """
<div class="tab-bar date-tabs" id="dateTabs">
"""
for i, dk in enumerate(all_dates):
    act = ' active' if i == 0 else ''
    html += f'  <div class="tab{act}" onclick="switchDateTab(\'{dk}\')">{date_shorts[dk]}</div>\n'
if monthly_data and monthly_data.get('days'):
    html += f'  <div class="tab" onclick="switchDateTab(\'results\')">結果</div>\n'
html += '</div>\n'

# ===== 各日付の中身 =====
for i, dk in enumerate(all_dates):
    act = ' active' if i == 0 else ''
    html += f'<div class="tab-content{act}" id="daytab-{dk}">\n'

    day_venues = sorted(set(p['race'].get('venue','') for p in preds if p['_date_key']==dk))
    day_graded = [p for p in preds if p['_date_key']==dk and p['grade'] in ('G1','G2','G3')]

    # サブタブ
    html += f'<div class="tab-bar sub-tabs" id="subTabs-{dk}">\n'
    html += f'  <div class="tab active" onclick="switchSubTab(\'{dk}\',\'picks\')">期待値あり</div>\n'
    if day_graded:
        html += f'  <div class="tab" onclick="switchSubTab(\'{dk}\',\'graded\')">今日の重賞</div>\n'
    for v in day_venues:
        html += f'  <div class="tab" onclick="switchSubTab(\'{dk}\',\'{v}\')">{v}</div>\n'
    html += f'  <div class="tab" onclick="switchSubTab(\'{dk}\',\'hotspot\')">注目データ</div>\n'
    html += f'  <div class="tab" onclick="switchSubTab(\'{dk}\',\'nakanohito\')">なかのひとたち</div>\n'
    html += '</div>\n'

    # ── 期待値あり ──
    day_buys = sorted([p for p in buy_preds if p['_date_key']==dk],
                      key=lambda p: p['race'].get('start_time', '99:99'))
    html += f'<div class="sub-content active" id="sub-{dk}-picks">\n'
    if alerts:
        html += '<div class="alert-banner">\n'
        html += '<div class="alert-title">ODDS UPDATE</div>\n'
        for a in alerts:
            html += f'<div class="alert-item">{a}</div>\n'
        html += '</div>\n'
    if day_buys:
        html += '<div class="section-title">期待値ありレース</div>\n'
        for p in day_buys:
            html += race_card_html(p, show_full=True)
    else:
        html += '<div class="section-title" style="color:var(--text-sub)">この日の期待値ありはありません</div>\n'

    # 軸馬一覧
    html += '<div class="section-title" style="margin-top:12px">AI軸馬一覧</div>\n'
    for v in day_venues:
        day_venue = [p for p in preds if p['_date_key']==dk and p['race'].get('venue','')==v]
        if not day_venue: continue
        day_venue.sort(key=lambda p: p['race'].get('race_num',0))
        html += f'<div class="jiku-section">\n'
        html += f'<button class="jiku-toggle" onclick="this.classList.toggle(\'open\');this.nextElementSibling.classList.toggle(\'open\')">'
        html += f'{v} 全{len(day_venue)}R<span class="arrow">\u25bc</span></button>\n'
        html += '<div class="jiku-list">\n'
        for p in day_venue:
            r = p['race']; is_buy = bool(p['buy_type'] or p['special_horse'])
            h = p['honmei']; rnum = r.get('race_num',0)
            rc = 'jiku-rnum has-bet' if is_buy else 'jiku-rnum'
            rowc = 'jiku-row bought' if is_buy else 'jiku-row'
            tag = ''
            if p['buy_type']: tag='<span class="jiku-tag">期待値</span>'
            elif p['special_horse']:
                rule = p['special_horse'].get('rule','')
                tag = '<span class="jiku-tag">スカウト</span>' if 'C2' in rule else '<span class="jiku-tag">覚醒</span>'
            html += f'<div class="{rowc}"><span class="{rc}">{rnum}R</span>'
            html += f'<span class="jiku-course">{r.get("surface","")}{r.get("distance","")}m</span>'
            html += f'<span class="jiku-mark">\u25ce</span>'
            html += f'<span class="jiku-horse">{h["horse_name"]}</span>{tag}'
            html += f'<span class="jiku-jockey">{h.get("jockey","")}</span>'
            html += f'<span class="jiku-score">{h["total_score"]:.1f}</span></div>\n'
        html += '</div></div>\n'
    html += '</div>\n'

    # ── 今週の重賞 ──
    if day_graded:
        html += f'<div class="sub-content" id="sub-{dk}-graded">\n'
        html += '<div class="section-title">今日の重賞</div>\n'
        for p in sorted(day_graded, key=lambda x: x['race'].get('race_num', 0)):
            html += race_card_html(p, show_full=True)
            g_comments = generate_graded_comments(p)
            r = p['race']
            html += '<div style="margin:-8px 16px 16px;background:linear-gradient(135deg,#F8F4F0,#FFF);border:2px solid var(--card-border);border-radius:12px;overflow:hidden">\n'
            html += f'<div style="padding:10px 16px;background:linear-gradient(135deg,var(--green),#4A6A40);color:white;font-size:12px;font-weight:700;letter-spacing:1px">ANALYSTS ROOM</div>\n'
            html += '<div class="comment-list">\n'
            for member_name, text in g_comments:
                info = MEMBERS[member_name]
                html += f'<div class="comment-row">\n'
                html += f'  <div class="comment-avatar" style="background:{info["color"]}20;border-color:{info["color"]}40">{info["icon"]}</div>\n'
                html += f'  <div class="comment-body">\n'
                html += f'    <div class="comment-name" style="color:{info["color"]}">{member_name}<span class="comment-role">{info["role"]}</span></div>\n'
                html += f'    <div class="comment-text">{text}</div>\n'
                html += f'  </div>\n'
                html += f'</div>\n'
            html += '</div></div>\n'
        html += '</div>\n'

    # ── 会場別 ──
    for v in day_venues:
        html += f'<div class="sub-content" id="sub-{dk}-{v}">\n'
        html += f'<div class="section-title">{v}競馬場</div>\n'
        venue_races = [p for p in preds if p['_date_key']==dk and p['race'].get('venue','')==v]
        for p in sorted(venue_races, key=lambda x: x['race'].get('race_num',0)):
            html += race_card_html(p, show_full=True)
        html += '</div>\n'

    # ── 注目データ ──
    html += f'<div class="sub-content" id="sub-{dk}-hotspot">\n'
    html += '<div class="section-title">注目データ</div>\n'
    html += '<div style="padding:4px 16px 8px;font-size:11px;color:var(--text-sub)">過去の回収率100%超パターンに該当する馬をピックアップ</div>\n'

    # Collect hotspot picks for this day
    day_hotspots = []
    for p in preds:
        if p['_date_key'] != dk: continue
        for hp in p.get('hotspot_picks', []):
            day_hotspots.append(hp)
    # 会場→レース番号順にソート
    venue_order = {v: i for i, v in enumerate(sorted(set(hp['venue'] for hp in day_hotspots)))} if day_hotspots else {}
    day_hotspots.sort(key=lambda x: (venue_order.get(x['venue'], 99), x['race_num']))

    if day_hotspots:
        current_venue = None
        for hp in day_hotspots:
            if hp['venue'] != current_venue:
                if current_venue is not None:
                    html += '</div>\n'  # close previous venue group
                current_venue = hp['venue']
                html += f'<div class="hotspot-venue-group">\n'
                html += f'<div class="hotspot-venue-label">{current_venue}</div>\n'

            stars = '★' * hp['best_conf']
            star_cls = 'hs-conf3' if hp['best_conf'] >= 3 else ('hs-conf2' if hp['best_conf'] >= 2 else 'hs-conf1')
            v6_tag = '<span class="hs-v6">v6◎</span>' if hp['is_v6_honmei'] else ('<span class="hs-v6buy">v6買い</span>' if hp['is_buy'] else '')

            html += f'<div class="hotspot-card">\n'
            html += f'  <div class="hs-header"><span class="hs-race">{hp["venue"]}{hp["race_num"]}R</span>'
            html += f'<span class="hs-horse">{hp["horse_name"].strip()}</span>'
            html += f'<span class="hs-odds">{hp["odds"]:.1f}倍</span>{v6_tag}</div>\n'

            for m in hp['matches']:
                m_stars = '★' * m['conf']
                html += f'  <div class="hs-match {star_cls}"><span class="hs-stars">{m_stars}</span>'
                html += f'<span class="hs-desc">{m["desc"]}</span>'
                html += f'<span class="hs-roi">単回{m["roi"]:.0f}%</span>'
                html += f'<span class="hs-n">n={m["n"]}</span></div>\n'
            html += '</div>\n'
        html += '</div>\n'  # close last venue group
    else:
        html += '<div style="padding:16px;text-align:center;color:var(--text-sub)">該当なし</div>\n'
    html += '</div>\n'

    # ── なかのひとたち ──
    html += f'<div class="sub-content" id="sub-{dk}-nakanohito">\n'
    html += '<div class="section-title">なかのひとたち</div>\n'
    html += '<div style="padding:8px 16px;font-size:12px;color:var(--text-sub);line-height:1.7">'
    html += 'NORISHICO AIの予想を支える7人のアナリスト。<br>期待値ありレースについて、それぞれの専門視点からコメントします。'
    html += '</div>\n'
    html += '<div style="display:flex;flex-wrap:wrap;gap:6px;padding:8px 16px;margin-bottom:8px">\n'
    for name, info in MEMBERS.items():
        html += f'<div style="display:flex;align-items:center;gap:4px;background:var(--cream);padding:4px 10px;border-radius:12px;font-size:11px">'
        html += f'<span>{info["icon"]}</span><span style="font-weight:700;color:{info["color"]}">{name}</span>'
        html += f'<span style="color:var(--text-sub)">{info["role"]}</span></div>\n'
    html += '</div>\n'
    day_buys_for_comment = [p for p in preds if p['_date_key']==dk and (p['buy_type'] or p['special_horse'])]
    if day_buys_for_comment:
        html += '<div class="nakanohito-section">\n'
        for p in day_buys_for_comment:
            r = p['race']
            comments = generate_member_comments(p)
            html += '<div class="nakanohito-race">\n'
            html += f'<div class="nakanohito-race-header"><span class="race-badge">{r.get("venue","")}{r.get("race_num",0)}R</span>{r.get("race_name","")}</div>\n'
            html += '<div class="comment-list">\n'
            for member_name, text in comments:
                info = MEMBERS[member_name]
                html += f'<div class="comment-row">\n'
                html += f'  <div class="comment-avatar" style="background:{info["color"]}20;border-color:{info["color"]}40">{info["icon"]}</div>\n'
                html += f'  <div class="comment-body">\n'
                html += f'    <div class="comment-name" style="color:{info["color"]}">{member_name}<span class="comment-role">{info["role"]}</span></div>\n'
                html += f'    <div class="comment-text">{text}</div>\n'
                html += f'  </div>\n'
                html += f'</div>\n'
            html += '</div></div>\n'
        html += '</div>\n'
    else:
        html += '<div style="padding:16px;text-align:center;color:var(--text-sub);font-size:13px">この日の期待値ありレースはありません</div>\n'
    html += '</div>\n'

    html += '</div>\n'  # daytab閉じ

# ===== 結果タブ =====
if monthly_data and monthly_data.get('days'):
    html += '<div class="tab-content" id="daytab-results">\n'
    mt = monthly_data.get('total', {})
    m_profit = mt.get('profit', 0)
    m_roi = mt.get('roi', 0)
    p_cls = 'rs-plus' if m_profit > 0 else ('rs-minus' if m_profit < 0 else 'rs-zero')
    r_cls = 'rs-plus' if m_roi > 100 else ('rs-minus' if m_roi < 100 else 'rs-zero')
    html += '<div class="result-summary">\n'
    html += f'<div class="rs-title">{monthly_data["month"].replace("_","/")} MONTHLY</div>\n'
    html += '<div class="rs-grid">\n'
    html += f'<div class="rs-item"><div class="rs-label">投資</div><div class="rs-value">{mt.get("cost",0):,}円</div></div>\n'
    html += f'<div class="rs-item"><div class="rs-label">回収</div><div class="rs-value">{mt.get("return",0):,}円</div></div>\n'
    html += f'<div class="rs-item"><div class="rs-label">収支</div><div class="rs-value {p_cls}">{m_profit:+,}円</div></div>\n'
    html += '</div>\n'
    html += '<div class="rs-grid" style="margin-top:8px">\n'
    html += f'<div class="rs-item"><div class="rs-label">ROI</div><div class="rs-value {r_cls}">{m_roi:.0f}%</div></div>\n'
    html += f'<div class="rs-item"><div class="rs-label">レース数</div><div class="rs-value">{mt.get("races",0)}R</div></div>\n'
    html += f'<div class="rs-item"><div class="rs-label">開催日数</div><div class="rs-value">{mt.get("days",0)}日</div></div>\n'
    html += '</div></div>\n'
    for day in reversed(monthly_data.get('days', [])):
        conds = day.get('track_conditions', {})
        html += '<div class="result-day">\n'
        html += '<div class="result-day-header">\n'
        html += f'<span class="day-label">{day["date"]}</span>\n'
        html += '<div class="cond-badges">'
        for venue, cond in sorted(conds.items()):
            html += f'<span class="cond-badge">{venue} {cond}</span>'
        html += '</div></div>\n'
        for br in day.get('buy_results', []):
            profit = br.get('profit', 0)
            p_cls2 = 'win' if profit > 0 else 'lose'
            detail_parts = [f'\u25ce{br.get("honmei","")} \u2192 {br.get("honmei_finish","?")}着']
            if br.get('ni'):
                detail_parts.append(f'\u25cb{br["ni"]} \u2192 {br.get("ni_finish","?")}着')
            detail_parts.append(f'1着: {br.get("winner","")}')
            detail = ' / '.join(detail_parts)
            html += '<div class="result-race">\n'
            html += f'<span class="rr-badge">{br.get("venue","")}{br.get("race_num",0)}R</span>\n'
            html += f'<div class="rr-body"><div class="rr-title">{br.get("race_name","")}</div>'
            html += f'<div class="rr-detail">{detail}</div></div>\n'
            html += f'<span class="rr-pnl {p_cls2}">{profit:+,}円</span>\n'
            html += '</div>\n'
        html += '</div>\n'
    html += '</div>\n'

# フッター + JS
html += f"""
<div class="footer">
  <p style="font-weight:700;color:var(--orange-pale);font-size:14px;letter-spacing:2px">NORISHICO AI v6.3</p>
  <p>生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
  <div class="disclaimer">
    想定オッズは確定前です。発走5分前にオッズ確定後、最終買い判定を行います。<br>
    本予想はAIによる自動判定です。馬券の購入は自己責任でお願いいたします。
  </div>
</div>

<script>
function fixStickyPositions() {{
  var header = document.querySelector('.sticky-header');
  var dateTabs = document.querySelector('.date-tabs');
  if (!header || !dateTabs) return;
  var hH = header.offsetHeight;
  dateTabs.style.top = hH + 'px';
  var dtH = dateTabs.offsetHeight;
  document.querySelectorAll('.sub-tabs').forEach(function(el) {{
    el.style.top = (hH + dtH) + 'px';
  }});
}}
window.addEventListener('load', fixStickyPositions);
window.addEventListener('resize', fixStickyPositions);

function switchDateTab(dk) {{
  document.querySelectorAll('#dateTabs .tab').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('daytab-' + dk).classList.add('active');
  fixStickyPositions();
  window.scrollTo(0, 0);
}}
function switchSubTab(dk, key) {{
  var parent = document.getElementById('daytab-' + dk);
  parent.querySelectorAll('.sub-tabs .tab').forEach(el => el.classList.remove('active'));
  parent.querySelectorAll('.sub-content').forEach(el => el.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('sub-' + dk + '-' + key).classList.add('active');
}}
</script>
</body></html>"""

outfile = 'this_week_prediction.html'
with open(outfile, 'w', encoding='utf-8') as f:
    f.write(html)
print(f"Generated: {outfile} ({len(html):,} bytes)")
print(f"Buy races: {len(buy_preds)}")
