"""アラートログ管理 — 買いレース変更の履歴を蓄積・取得"""
import json
from pathlib import Path
from datetime import datetime

ALERTS_FILE = Path(__file__).parent / 'alerts_log.json'


def load_alerts() -> list:
    """現在のアラートログを読み込み（日付が変わったら自動クリア）"""
    if not ALERTS_FILE.exists():
        return []
    try:
        data = json.loads(ALERTS_FILE.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, ValueError):
        return []
    # 日付チェック: ログの日付と今日が違えば自動クリア
    if data and data[0].get('date') != datetime.now().strftime('%Y-%m-%d'):
        save_alerts([])
        return []
    return data


def save_alerts(alerts: list):
    """アラートログを保存"""
    ALERTS_FILE.write_text(
        json.dumps(alerts, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )


def clear_alerts():
    """ログをクリア"""
    save_alerts([])


def append_alerts(new_items: list):
    """アラートを追記（タイムスタンプ付き）

    Args:
        new_items: [{'type': '追加'|'除外'|'変更', 'text': str, 'venue': str, 'race_num': int}]
    """
    if not new_items:
        return
    alerts = load_alerts()
    now = datetime.now()
    for item in new_items:
        alerts.append({
            'date': now.strftime('%Y-%m-%d'),
            'time': now.strftime('%H:%M'),
            'type': item['type'],
            'text': item['text'],
        })
    save_alerts(alerts)


def compare_and_log(prev_preds: list, curr_preds: list) -> list:
    """前回と今回の予想を比較し、差分をアラートログに追記

    Returns: 新規アラートのリスト
    """
    prev_buys = {}
    for p in prev_preds:
        rid = p['race']['race_id']
        bt = p.get('buy_type', '')
        sp = p.get('special_horse')
        if bt or sp:
            prev_buys[rid] = {
                'buy_type': bt or 'special',
                'race': p['race'],
                'mark': p.get('mark', ''),
            }

    curr_buys = {}
    for p in curr_preds:
        rid = p['race']['race_id']
        bt = p.get('buy_type', '')
        sp = p.get('special_horse')
        if bt or sp:
            curr_buys[rid] = {
                'buy_type': bt or 'special',
                'race': p['race'],
                'mark': p.get('mark', ''),
            }

    new_alerts = []

    # 新規追加
    for rid in curr_buys:
        if rid not in prev_buys:
            r = curr_buys[rid]['race']
            label = f"{r.get('venue','')}{r.get('race_num','')}R {r.get('race_name','')}"
            now_str = datetime.now().strftime('%H:%M')
            new_alerts.append({
                'type': '追加',
                'race_id': rid,
                'text': f"🆕 {label} が期待値ありに追加（{now_str}時点）",
            })

    # 除外
    for rid in prev_buys:
        if rid not in curr_buys:
            r = prev_buys[rid]['race']
            label = f"{r.get('venue','')}{r.get('race_num','')}R {r.get('race_name','')}"
            now_str = datetime.now().strftime('%H:%M')
            new_alerts.append({
                'type': '除外',
                'race_id': rid,
                'text': f"❌ {label} → 発走前オッズ確認で条件外に変更（{now_str}更新）",
            })

    # 買い方変更
    for rid in set(prev_buys) & set(curr_buys):
        old_bt = prev_buys[rid]['buy_type']
        new_bt = curr_buys[rid]['buy_type']
        if old_bt != new_bt:
            r = curr_buys[rid]['race']
            label = f"{r.get('venue','')}{r.get('race_num','')}R {r.get('race_name','')}"
            type_labels = {
                'v6_normal': '単勝+馬連',
                'v6_challenge': '単勝のみ(チャレンジ)',
                'special': '別枠(単勝)',
            }
            old_label = type_labels.get(old_bt, old_bt)
            new_label = type_labels.get(new_bt, new_bt)
            new_alerts.append({
                'type': '変更',
                'race_id': rid,
                'text': f"⚠ {label} 買い目変更: {old_label} → {new_label}",
            })

    if new_alerts:
        append_alerts(new_alerts)

    return new_alerts
