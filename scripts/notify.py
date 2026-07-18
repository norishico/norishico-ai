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


NOTIFY_PAUSED_FLAG = PROJ / 'notify_paused.flag'

def _send(content='', embeds=None):
    if NOTIFY_PAUSED_FLAG.exists():
        print('[notify] paused (notify_paused.flag exists), skip')
        return False
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


def notify_morning_summary(preds):
    """朝9時: 今日の対象レース一覧通知"""
    buy_races = [p for p in preds if p.get('buy_type') or p.get('special_horse')]
    if not buy_races:
        _send("🐴 **今日の対象レース**\n\n現時点で期待値通過レースはありません。\nオッズ変動で追加されたら改めて通知します。")
        return

    # start_time順にソート
    buy_races.sort(key=lambda x: x.get('race',{}).get('start_time',''))

    lines = [f"🐴 **今日はこのレースが対象です（{len(buy_races)}R）**\n"]
    for p in buy_races:
        r = p.get('race', {})
        h = p.get('honmei', {})
        sp = p.get('special_horse')
        bt = p.get('buy_type') or ''
        focus = sp if sp else h
        venue = r.get('venue', '')
        rnum = r.get('race_num', '')
        rname = r.get('race_name', '')
        stime = r.get('start_time', '')
        name = focus.get('horse_name', '')
        odds = focus.get('odds', 0) or 0
        num = focus.get('horse_num', '')
        mark = '★★★' if bt == 'v6_star3' else '★★' if bt == 'v6_star2' else '★'
        lines.append(f"  {mark} {stime} {venue}{rnum}R {rname}")
        lines.append(f"     ◎{_umaban(num)} {name} {odds:.1f}倍")

    lines.append("\n発走10分前に最終判定します。オッズ変動で追加/除外があれば都度通知します。")
    _send('\n'.join(lines))


# 後方互換のため旧関数名も残す(内部で新関数へ委譲)
def notify_morning_check(preds):
    notify_morning_summary(preds)


def notify_added(pred):
    """オッズ変動で新たに対象入りしたレースの通知"""
    r = pred.get('race', {})
    h = pred.get('honmei', {})
    sp = pred.get('special_horse')
    focus = sp if sp else h
    venue = r.get('venue', '')
    rnum = r.get('race_num', '')
    rname = r.get('race_name', '')
    stime = r.get('start_time', '')
    name = focus.get('horse_name', '')
    odds = focus.get('odds', 0) or 0
    num = focus.get('horse_num', '')

    embed = {
        'title': '➕ 対象に追加されました',
        'color': 0xFBC02D,  # 黄色
        'fields': [
            {'name': 'レース', 'value': f'{venue}{rnum}R {rname}（発走 {stime}）', 'inline': False},
            {'name': '◎ 本命', 'value': f'**{_umaban(num)} {name}** {odds:.1f}倍', 'inline': False},
            {'name': '備考', 'value': '朝時点では対象外でしたが、オッズ変動で買い条件を満たしました。発走10分前に最終判定します。', 'inline': False},
        ],
        'timestamp': datetime.utcnow().isoformat(),
    }
    _send(embeds=[embed])


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
            if 0 < mins <= 60:
                remaining = f'（あと{mins}分）'
        except Exception:
            pass

    embed = {
        'title': f'🎯 買います（最終確定）',
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


def notify_sanrenpuku_jiku(candidates, week_label='今週'):
    """三連複軸馬候補通知（金曜夜）

    Args:
        candidates: calc_sanrenpuku_jiku.calc_jiku_candidates() の戻り値
        week_label: '今週' など
    """
    if not candidates:
        _send(f'🎴 **{week_label}の三連複軸馬候補: 見送り**\n（差し・中団でMC50%超のOP馬なし）')
        return

    lines = [f'🎴 **{week_label}の三連複軸馬候補** ({len(candidates)}件)\n']
    for c in candidates:
        j = c['jiku']
        mc_pct = int(j['mc_rate'] * 100)
        star = '🔴' if mc_pct >= 70 else ('🟡' if mc_pct >= 60 else '🟢')
        day = '土' if c['date'].endswith('-20') or c['date'][-2:] in ('06','13','20','27') else '日'
        # 実際は曜日をdateから判定
        from datetime import datetime
        try:
            wd = datetime.strptime(c['date'], '%Y-%m-%d').weekday()
            day = '土' if wd == 5 else ('日' if wd == 6 else c['date'][-5:])
        except Exception:
            pass

        lines.append(
            f'{star} [{day}] **{c["venue"]} {c["race_name"]}** '
            f'({c["grade"]} 芝{c["distance"]}m)'
        )
        lines.append(
            f'　🎯 軸: **{j["name"]}** ({j["style"]}) '
            f'MC **{mc_pct}%** / {j["odds"]}倍'
        )
        aite_str = '　相手: ' + ' / '.join(
            f'{a["name"]}({a["odds"]}倍)' for a in c['aite']
        )
        lines.append(aite_str)
        pts = len(c['aite']) * (len(c['aite']) - 1) // 2
        lines.append(f'　💴 三連複 {pts}点 × 100円 = {pts*100}円')
        lines.append('')

    lines.append('※ 条件: 差し+中団 × MC≥50% × 阪神函館除外 × OPクラス')
    _send('\n'.join(lines))


if __name__ == '__main__':
    print('Discord notify test...')
    ok = _send('✅ Discord通知テスト — 正常に動作しています')
    print('sent!' if ok else 'failed!')
