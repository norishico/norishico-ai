"""Discord Webhook 通知モジュール

通知の発射規則(2026-04-18 委員会決定で整理):
  1. notify_prediction_ready  - 予想公開 (金曜21:00=土曜予想 / 土曜11:00=日曜予想)  ※1日1回
  2. notify_morning_check     - 【廃止】朝オッズ確認は内部処理、通知しない(通知過多対策)
  3. notify_buy_go            - 買いGO (発走10分前) ※該当レースのみ
  4. notify_cancelled         - 見送り通知 (オッズ変動で条件外化したレースのみ)
  5. notify_daily_result      - 本日結果 (夜1回)
"""
import json
import os
import urllib.request
from pathlib import Path
from datetime import datetime

PROJ = Path(__file__).resolve().parent.parent

_CIRCLED = ['⓪','①','②','③','④','⑤','⑥','⑦','⑧','⑨','⑩','⑪','⑫','⑬','⑭','⑮','⑯','⑰','⑱']
def _umaban(n):
    try:
        i = int(n)
        return _CIRCLED[i] if 0 <= i < len(_CIRCLED) else str(i)
    except (TypeError, ValueError):
        return str(n)

def _get_webhook_url():
    env_path = PROJ / '.env'
    if env_path.exists():
        for line in env_path.read_text(encoding='utf-8').splitlines():
            if line.startswith('DISCORD_WEBHOOK_URL='):
                return line.split('=', 1)[1].strip()
    return os.environ.get('DISCORD_WEBHOOK_URL', '')


def _send(content='', embeds=None):
    url = _get_webhook_url()
    if not url:
        print('[notify] DISCORD_WEBHOOK_URL not set, skip')
        return False
    payload = {'content': content}
    if embeds:
        payload['embeds'] = embeds
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'application/json',
        'User-Agent': 'NorishikoAI/1.0',
    })
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f'[notify] send failed: {e}')
        return False


def notify_prediction_ready(preds, day_label=''):
    """予想公開通知"""
    day_label = {'sat': '土曜予想', 'sun': '日曜予想', 'mon': '月曜予想'}.get(day_label, day_label)
    buy_races = [p for p in preds if p.get('buy_type') or p.get('special_horse')]
    if not buy_races:
        _send(f"📋 {day_label} 予想生成完了（買い推奨 0R）")
        return

    lines = [f"📋 **{day_label} 予想が出ました（{len(buy_races)}R買い推奨）**\n"]
    for p in buy_races:
        r = p.get('race', {})
        h = p.get('honmei', {})
        bt = p.get('buy_type', '')
        sp = p.get('special_horse')
        stars = '★★★' if bt == 'v6_star3' else '★★' if bt == 'v6_star2' else '★'
        venue = r.get('venue', '')
        rnum = r.get('race_num', '')
        rname = r.get('race_name', '')
        stime = r.get('start_time', '')
        odds = h.get('odds', 0) or 0
        name = h.get('horse_name', '')
        jockey = h.get('jockey', '')
        num = h.get('horse_num', '')
        if sp:
            name = sp.get('horse_name', '')
            odds = sp.get('odds', 0) or 0
            jockey = sp.get('jockey', '')
            num = sp.get('horse_num', '')
            stars = '★'
        lines.append(f"  {stars} {venue}{rnum}R {rname} {stime}")
        lines.append(f"     ◎{_umaban(num)} {name} {odds:.1f}倍 ({jockey})")

    lines.append(f"\n🔗 https://westr.github.io/norishiko-ai/")
    _send('\n'.join(lines))


def notify_morning_check(preds):
    """朝オッズ確認通知"""
    buy_races = [p for p in preds if p.get('buy_type') or p.get('special_horse')]
    lines = ["🔄 **朝のオッズ確認完了**\n"]
    # ブランド名は買いGOの footer のみに表示
    for p in buy_races:
        r = p.get('race', {})
        h = p.get('honmei', {})
        mm = p.get('momentum', {})
        venue = r.get('venue', '')
        rnum = r.get('race_num', '')
        name = h.get('horse_name', '')
        label = mm.get('label', '→安定') if mm else ''
        chg = mm.get('change_pct', 0) if mm else 0
        init = mm.get('initial_odds', 0) if mm else 0
        cur = h.get('odds', 0) or 0
        lines.append(f"  {venue}{rnum}R ◎{name} {init:.1f}→{cur:.1f}倍 {label}")

    lines.append("\n発走前に最終判定します")
    _send('\n'.join(lines))


def notify_buy_go(pred):
    """買いGO通知（発走10分前）"""
    r = pred.get('race', {})
    h = pred.get('honmei', {})
    ni = pred.get('ni', {})
    bt = pred.get('buy_type', '')
    sp = pred.get('special_horse')
    mm = pred.get('momentum', {})
    sb = h.get('_score_breakdown', {})

    venue = r.get('venue', '')
    rnum = r.get('race_num', '')
    rname = r.get('race_name', '')
    stime = r.get('start_time', '')
    odds = h.get('odds', 0) or 0
    name = h.get('horse_name', '')
    jockey = h.get('jockey', '')

    # 「あと○分」計算
    remaining = ''
    if stime:
        try:
            now = datetime.now()
            race_dt = datetime.strptime(f"{now.strftime('%Y-%m-%d')} {stime}", '%Y-%m-%d %H:%M')
            mins = int((race_dt - now).total_seconds() / 60)
            if mins > 0:
                remaining = f'（あと{mins}分）'
        except Exception:
            pass

    embed = {
        'title': f'✅ 買いGO（直前確認済み）',
        'color': 0x2E7D32,
        'fields': [
            {'name': 'レース', 'value': f'{venue}{rnum}R {rname}', 'inline': False},
            {'name': '◎ 本命', 'value': f'**{_umaban(h.get("horse_num",""))} {name}** {odds:.1f}倍 ({jockey})', 'inline': True},
        ],
        'footer': {'text': f'発走 {stime}{remaining} | NORISHICO KEIBA AI'},
        'timestamp': datetime.utcnow().isoformat(),
    }

    if ni:
        ni_name = ni.get('horse_name', '')
        ni_odds = ni.get('odds', 0) or 0
        embed['fields'].append({'name': '○ 対抗', 'value': f'{_umaban(ni.get("horse_num",""))} {ni_name} {ni_odds:.1f}倍', 'inline': True})

    if bt == 'v6_challenge' or sp:
        embed['fields'].append({'name': '買い目', 'value': '単勝◎ 1,000円', 'inline': False})
    elif odds >= 8:
        embed['fields'].append({'name': '買い目', 'value': '単勝◎500円 + 馬連◎○1,500円 = 2,000円', 'inline': False})
    else:
        embed['fields'].append({'name': '買い目', 'value': '単勝◎1,000円 + 馬連◎○1,000円 = 2,000円', 'inline': False})

    if mm and mm.get('label'):
        embed['fields'].append({'name': '市場動向', 'value': mm['label'], 'inline': True})

    if sb:
        parts = [f"基礎{sb.get('base',0):.0f}"]
        for key, label in [('venue_sire','コース父'), ('cushion_sire','馬場父'),
                           ('nicks','ニックス'), ('course_blood','血統相乗')]:
            v = sb.get(key, 0)
            if v: parts.append(f'{label}{v:+.1f}')
        embed['fields'].append({'name': '内訳', 'value': ' '.join(parts), 'inline': False})

    _send(embeds=[embed])


def notify_cancelled(pred, reason=''):
    """見送り通知"""
    r = pred.get('race', {})
    h = pred.get('honmei', {})
    venue = r.get('venue', '')
    rnum = r.get('race_num', '')
    rname = r.get('race_name', '')
    name = h.get('horse_name', '')
    odds = h.get('odds', 0) or 0

    _send(f"⚠️ **見送りに変更**\n\n{venue}{rnum}R {rname}\n◎{name} {odds:.1f}倍\n理由: {reason}")


def notify_daily_result(buy_results, total_cost=0, total_return=0):
    """本日結果通知

    Args:
        buy_results: list of {'venue','race_num','race_name','honmei','finish','ret','cost'}
        total_cost: 総投資
        total_return: 総回収
    """
    if not buy_results:
        _send("📊 本日は買い推奨レースなし")
        return

    profit = total_return - total_cost
    roi = total_return / total_cost * 100 if total_cost > 0 else 0
    hit_count = sum(1 for r in buy_results if r.get('ret', 0) > 0)

    lines = [f"📊 **本日の結果**\n"]
    lines.append(f"**{len(buy_results)}R買い → {hit_count}R的中** {'🎯' if hit_count else ''}\n")

    for r in buy_results:
        venue = r.get('venue', '')
        rnum = r.get('race_num', '')
        name = r.get('honmei', '')
        finish = r.get('finish', 99)
        ret = r.get('ret', 0)
        if ret > 0:
            lines.append(f"  ✅ {venue}{rnum}R ◎{name} {finish}着！ **+{ret-r.get('cost',0):,}円**")
        else:
            lines.append(f"  ❌ {venue}{rnum}R ◎{name} {finish}着")

    emoji = '🎉' if profit > 0 else '😤'
    lines.append(f"\n{emoji} **本日損益: {profit:+,}円 (ROI {roi:.0f}%)**")
    _send('\n'.join(lines))


if __name__ == '__main__':
    print('Discord notify test...')
    ok = _send('✅ Discord通知テスト — 正常に動作しています')
    print('sent!' if ok else 'failed!')
