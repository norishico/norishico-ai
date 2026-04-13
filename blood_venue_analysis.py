"""血統×会場・枠順データ集計 → HTMLランキング出力"""
import sqlite3
from collections import defaultdict
from datetime import datetime

conn = sqlite3.connect('keiba.db')
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA cache_size=-65536")
conn.execute("PRAGMA temp_store=MEMORY")
conn.execute("PRAGMA mmap_size=268435456")

# 全データ取得
print("Loading results...")
rows = conn.execute("""
    SELECT venue, distance, surface, track_cond, sire, finish, odds, umaban, num_horses, date
    FROM results
    WHERE finish IS NOT NULL AND finish < 90 AND finish > 0
      AND odds IS NOT NULL AND odds > 0
      AND sire IS NOT NULL AND TRIM(sire) != ''
      AND umaban > 0 AND num_horses > 0
""").fetchall()
print(f"Loaded {len(rows)} rows")

def gate_group(umaban, num_horses):
    """馬番→枠番グループ（JRA方式の近似）"""
    if num_horses <= 0: return None
    ratio = umaban / num_horses
    if ratio <= 0.375:   return '1-3枠(内)'
    elif ratio <= 0.750: return '4-6枠(中)'
    else:                return '7-8枠(外)'

# ── ①会場×距離×父馬名別 ──
print("Calculating ① venue x distance x sire...")
vds = defaultdict(lambda: [0, 0, 0, 0.0])  # [total, top3, win, sum_odds_if_win]
for r in rows:
    sire = r['sire'].strip()
    key = (r['venue'], r['distance'], sire)
    vds[key][0] += 1
    if r['finish'] <= 3: vds[key][1] += 1
    if r['finish'] == 1:
        vds[key][2] += 1
        vds[key][3] += r['odds']

results_1 = []
for (venue, dist, sire), (n, t3, wins, odds_sum) in vds.items():
    if n < 20: continue
    t3r = t3 / n * 100
    tan_roi = odds_sum / n * 100  # 単勝回収率(100円あたり)
    if t3r >= 80 or tan_roi >= 150:
        results_1.append({
            'venue': venue, 'dist': dist, 'sire': sire,
            'n': n, 'wins': wins, 'top3': t3, 'top3_rate': round(t3r, 1),
            'tan_roi': round(tan_roi, 1),
        })

results_1.sort(key=lambda x: (-x['top3_rate'], -x['tan_roi']))
print(f"  ① {len(results_1)} patterns found")

# ── ②枠番グループ×父馬名別 ──
print("Calculating ② gate group x sire...")
gs = defaultdict(lambda: [0, 0, 0, 0.0])
for r in rows:
    sire = r['sire'].strip()
    gg = gate_group(r['umaban'], r['num_horses'])
    if gg is None: continue
    key = (gg, sire)
    gs[key][0] += 1
    if r['finish'] <= 3: gs[key][1] += 1
    if r['finish'] == 1:
        gs[key][2] += 1
        gs[key][3] += r['odds']

results_2 = []
for (gg, sire), (n, t3, wins, odds_sum) in gs.items():
    if n < 15: continue
    t3r = t3 / n * 100
    tan_roi = odds_sum / n * 100
    if t3r >= 80 or tan_roi >= 150:
        results_2.append({
            'gate': gg, 'sire': sire,
            'n': n, 'wins': wins, 'top3': t3, 'top3_rate': round(t3r, 1),
            'tan_roi': round(tan_roi, 1),
        })

results_2.sort(key=lambda x: (-x['top3_rate'], -x['tan_roi']))
print(f"  ② {len(results_2)} patterns found")

# ── ③馬場状態×父馬名別（重・不良限定）──
print("Calculating ③ track_cond(重/不良) x sire...")
ts = defaultdict(lambda: [0, 0, 0, 0.0])
for r in rows:
    cond = r['track_cond']
    if cond not in ('重', '不'): continue
    sire = r['sire'].strip()
    cond_label = '重' if cond == '重' else '不良'
    key = (cond_label, sire)
    ts[key][0] += 1
    if r['finish'] <= 3: ts[key][1] += 1
    if r['finish'] == 1:
        ts[key][2] += 1
        ts[key][3] += r['odds']

results_3 = []
for (cond, sire), (n, t3, wins, odds_sum) in ts.items():
    if n < 10: continue
    t3r = t3 / n * 100
    tan_roi = odds_sum / n * 100
    if t3r >= 80 or tan_roi >= 150:
        results_3.append({
            'cond': cond, 'sire': sire,
            'n': n, 'wins': wins, 'top3': t3, 'top3_rate': round(t3r, 1),
            'tan_roi': round(tan_roi, 1),
        })

results_3.sort(key=lambda x: (-x['top3_rate'], -x['tan_roi']))
print(f"  ③ {len(results_3)} patterns found")

# ── HTML出力 ──
print("Generating HTML...")

def make_bar(val, max_val=200, color='#4CAF50'):
    w = min(val / max_val * 100, 100)
    return f'<div style="background:{color};width:{w}%;height:18px;border-radius:3px;display:inline-block"></div>'

def roi_color(roi):
    if roi >= 200: return '#e74c3c'
    if roi >= 150: return '#e67e22'
    if roi >= 100: return '#27ae60'
    return '#95a5a6'

def t3_color(t3r):
    if t3r >= 80: return '#e74c3c'
    if t3r >= 50: return '#e67e22'
    if t3r >= 30: return '#27ae60'
    return '#95a5a6'

html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>血統×会場・枠順データ ランキング</title>
<style>
body {{ font-family: 'Segoe UI', 'Yu Gothic UI', sans-serif; margin: 20px; background: #f5f5f5; }}
h1 {{ color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }}
h2 {{ color: #34495e; margin-top: 40px; }}
table {{ border-collapse: collapse; width: 100%; background: white; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 30px; }}
th {{ background: #2c3e50; color: white; padding: 10px 8px; text-align: center; font-size: 13px; }}
td {{ padding: 8px; border-bottom: 1px solid #ecf0f1; text-align: center; font-size: 13px; }}
tr:hover {{ background: #ebf5fb; }}
.rank {{ font-weight: bold; color: #7f8c8d; }}
.sire {{ text-align: left; font-weight: bold; }}
.highlight {{ background: #ffeaa7; font-weight: bold; }}
.roi-high {{ color: #e74c3c; font-weight: bold; }}
.roi-mid {{ color: #e67e22; font-weight: bold; }}
.roi-ok {{ color: #27ae60; }}
.bar-container {{ width: 80px; display: inline-block; }}
.summary {{ background: #dfe6e9; padding: 15px; border-radius: 8px; margin-bottom: 20px; font-size: 14px; }}
.note {{ color: #7f8c8d; font-size: 12px; margin-top: 5px; }}
</style>
</head>
<body>
<h1>血統×会場・枠順データ ランキング</h1>
<div class="summary">
  生成日: {datetime.now().strftime('%Y-%m-%d %H:%M')} ｜ 対象: keiba.db 全{len(rows):,}件 ｜
  抽出条件: 複勝率80%以上 または 単勝回収率150%以上
</div>
"""

# ① 会場×距離×父馬名
html += '<h2>① 会場×距離×父馬名 複勝率・回収率ランキング</h2>\n'
html += '<p class="note">サンプル20件以上</p>\n'
html += '<table>\n<tr><th>#</th><th>会場</th><th>距離</th><th>父馬名</th><th>件数</th><th>勝数</th><th>3着内</th><th>複勝率</th><th></th><th>単勝回収率</th><th></th></tr>\n'
for i, r in enumerate(results_1[:100], 1):
    t3c = 'roi-high' if r['top3_rate'] >= 80 else 'roi-mid' if r['top3_rate'] >= 50 else 'roi-ok'
    rc = 'roi-high' if r['tan_roi'] >= 200 else 'roi-mid' if r['tan_roi'] >= 150 else 'roi-ok' if r['tan_roi'] >= 100 else ''
    hl = ' class="highlight"' if r['top3_rate'] >= 80 and r['tan_roi'] >= 150 else ''
    html += f'<tr{hl}><td class="rank">{i}</td><td>{r["venue"]}</td><td>{r["dist"]}m</td><td class="sire">{r["sire"]}</td>'
    html += f'<td>{r["n"]}</td><td>{r["wins"]}</td><td>{r["top3"]}</td>'
    html += f'<td class="{t3c}">{r["top3_rate"]}%</td><td>{make_bar(r["top3_rate"], 100, t3_color(r["top3_rate"]))}</td>'
    html += f'<td class="{rc}">{r["tan_roi"]}%</td><td>{make_bar(r["tan_roi"], 300, roi_color(r["tan_roi"]))}</td></tr>\n'
html += '</table>\n'

# ② 枠番グループ×父馬名
html += '<h2>② 枠番グループ×父馬名 複勝率ランキング</h2>\n'
html += '<p class="note">サンプル15件以上 ｜ 枠番グループ: 1-3枠(内)=馬番上位37.5%、4-6枠(中)=中間、7-8枠(外)=下位25%</p>\n'
html += '<table>\n<tr><th>#</th><th>枠番</th><th>父馬名</th><th>件数</th><th>勝数</th><th>3着内</th><th>複勝率</th><th></th><th>単勝回収率</th><th></th></tr>\n'
for i, r in enumerate(results_2[:100], 1):
    t3c = 'roi-high' if r['top3_rate'] >= 80 else 'roi-mid' if r['top3_rate'] >= 50 else 'roi-ok'
    rc = 'roi-high' if r['tan_roi'] >= 200 else 'roi-mid' if r['tan_roi'] >= 150 else 'roi-ok' if r['tan_roi'] >= 100 else ''
    hl = ' class="highlight"' if r['top3_rate'] >= 80 and r['tan_roi'] >= 150 else ''
    html += f'<tr{hl}><td class="rank">{i}</td><td>{r["gate"]}</td><td class="sire">{r["sire"]}</td>'
    html += f'<td>{r["n"]}</td><td>{r["wins"]}</td><td>{r["top3"]}</td>'
    html += f'<td class="{t3c}">{r["top3_rate"]}%</td><td>{make_bar(r["top3_rate"], 100, t3_color(r["top3_rate"]))}</td>'
    html += f'<td class="{rc}">{r["tan_roi"]}%</td><td>{make_bar(r["tan_roi"], 300, roi_color(r["tan_roi"]))}</td></tr>\n'
html += '</table>\n'

# ③ 馬場状態×父馬名
html += '<h2>③ 馬場状態(重・不良)×父馬名 複勝率ランキング</h2>\n'
html += '<p class="note">サンプル10件以上 ｜ 重馬場・不良馬場限定</p>\n'
html += '<table>\n<tr><th>#</th><th>馬場</th><th>父馬名</th><th>件数</th><th>勝数</th><th>3着内</th><th>複勝率</th><th></th><th>単勝回収率</th><th></th></tr>\n'
for i, r in enumerate(results_3[:100], 1):
    t3c = 'roi-high' if r['top3_rate'] >= 80 else 'roi-mid' if r['top3_rate'] >= 50 else 'roi-ok'
    rc = 'roi-high' if r['tan_roi'] >= 200 else 'roi-mid' if r['tan_roi'] >= 150 else 'roi-ok' if r['tan_roi'] >= 100 else ''
    hl = ' class="highlight"' if r['top3_rate'] >= 80 and r['tan_roi'] >= 150 else ''
    html += f'<tr{hl}><td class="rank">{i}</td><td>{r["cond"]}</td><td class="sire">{r["sire"]}</td>'
    html += f'<td>{r["n"]}</td><td>{r["wins"]}</td><td>{r["top3"]}</td>'
    html += f'<td class="{t3c}">{r["top3_rate"]}%</td><td>{make_bar(r["top3_rate"], 100, t3_color(r["top3_rate"]))}</td>'
    html += f'<td class="{rc}">{r["tan_roi"]}%</td><td>{make_bar(r["tan_roi"], 300, roi_color(r["tan_roi"]))}</td></tr>\n'
html += '</table>\n'

html += """
<div class="note" style="margin-top:40px">
  <p>※ 複勝率 = 3着以内率（着順1-3の割合）</p>
  <p>※ 単勝回収率 = 100円あたりの回収額（勝った場合のオッズ合計÷件数×100）</p>
  <p>※ 黄色ハイライト = 複勝率80%以上 かつ 単勝回収率150%以上（両方達成）</p>
</div>
</body>
</html>"""

with open('blood_venue_ranking.html', 'w', encoding='utf-8') as f:
    f.write(html)

print(f"\n=== 完了 ===")
print(f"  ① 会場×距離×父馬名: {len(results_1)}件")
print(f"  ② 枠番グループ×父馬名: {len(results_2)}件")
print(f"  ③ 馬場状態×父馬名: {len(results_3)}件")
print(f"  → blood_venue_ranking.html に出力しました")
