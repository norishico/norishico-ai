"""血統データレポートHTML生成"""
import sqlite3, json, sys
sys.stdout.reconfigure(encoding='utf-8')

conn = sqlite3.connect('keiba.db')
conn.execute('PRAGMA cache_size=-65536')
conn.execute('PRAGMA temp_store=MEMORY')

BASE = "finish > 0 AND finish < 90 AND num_horses >= 8 AND odds >= 3 AND sire IS NOT NULL AND TRIM(sire) != ''"

def q(sql):
    return conn.execute(sql).fetchall()

# Collect all data
sections = []

# 1. ニックス
rows = q(f"""SELECT TRIM(sire), TRIM(dam_sire), COUNT(*),
    SUM(CASE WHEN finish=1 THEN 1 ELSE 0 END),
    ROUND(SUM(CASE WHEN finish=1 THEN odds ELSE 0 END)*100.0/COUNT(*), 0)
FROM results WHERE {BASE} AND dam_sire IS NOT NULL
GROUP BY TRIM(sire), TRIM(dam_sire) HAVING COUNT(*) >= 50
ORDER BY 5 DESC LIMIT 20""")
sections.append(('nicks', '父×母父ニックス（相性抜群の組み合わせ）',
    'n≥50、単勝回収率順。この父とこの母父の組み合わせは統計的に好走率が高い。',
    [{'sire':r[0],'dam_sire':r[1],'n':r[2],'wins':r[3],'roi':r[4]} for r in rows]))

# 2. 母父パワー
rows = q(f"""SELECT TRIM(dam_sire), COUNT(*), SUM(CASE WHEN finish=1 THEN 1 ELSE 0 END),
    ROUND(SUM(CASE WHEN finish=1 THEN odds ELSE 0 END)*100.0/COUNT(*), 0)
FROM results WHERE {BASE.replace('sire','dam_sire')}
GROUP BY 1 HAVING COUNT(*) >= 300 ORDER BY 4 DESC LIMIT 15""")
sections.append(('dam_sire', '母父だけで回収率100%超',
    'n≥300。母父の血が強く影響するパターン。',
    [{'name':r[0],'n':r[1],'wins':r[2],'roi':r[3]} for r in rows]))

# 3. 牝馬限定
rows = q(f"""SELECT TRIM(sire), COUNT(*),
    ROUND(SUM(CASE WHEN finish=1 THEN odds ELSE 0 END)*100.0/COUNT(*), 0)
FROM results WHERE {BASE} AND sex='牝' AND race_name LIKE '%牝%'
GROUP BY 1 HAVING COUNT(*) >= 80 ORDER BY 3 DESC LIMIT 15""")
sections.append(('himba', '牝馬限定戦の隠れ名血',
    '牝馬限定レースで特に成績が良い種牡馬。',
    [{'name':r[0],'n':r[1],'roi':r[2]} for r in rows]))

# 4. 新馬戦
rows = q(f"""SELECT TRIM(sire), COUNT(*),
    ROUND(SUM(CASE WHEN finish=1 THEN odds ELSE 0 END)*100.0/COUNT(*), 0)
FROM results WHERE {BASE} AND race_name LIKE '%新馬%'
GROUP BY 1 HAVING COUNT(*) >= 50 ORDER BY 3 DESC LIMIT 15""")
sections.append(('shinba', '新馬戦で狙える血統',
    '初出走の新馬戦で単勝回収率が高い種牡馬。',
    [{'name':r[0],'n':r[1],'roi':r[2]} for r in rows]))

# 5. 重馬場
rows = q(f"""SELECT TRIM(sire),
    COALESCE(SUM(CASE WHEN surface LIKE '%芝%' AND track_cond='重' AND finish=1 THEN odds ELSE 0 END)*100.0/
      NULLIF(SUM(CASE WHEN surface LIKE '%芝%' AND track_cond='重' THEN 1 ELSE 0 END),0), 0),
    COALESCE(SUM(CASE WHEN surface NOT LIKE '%芝%' AND track_cond='重' AND finish=1 THEN odds ELSE 0 END)*100.0/
      NULLIF(SUM(CASE WHEN surface NOT LIKE '%芝%' AND track_cond='重' THEN 1 ELSE 0 END),0), 0),
    SUM(CASE WHEN track_cond='重' THEN 1 ELSE 0 END)
FROM results WHERE {BASE}
GROUP BY 1 HAVING SUM(CASE WHEN track_cond='重' THEN 1 ELSE 0 END) >= 50
ORDER BY 2 DESC LIMIT 15""")
sections.append(('heavy', '重馬場の芝×ダート対照表',
    '同じ種牡馬でも芝の重とダートの重で成績が真逆になることがある。',
    [{'name':r[0],'turf':r[1],'dirt':r[2],'n':r[3]} for r in rows]))

# 6. 覚醒型
rows = q(f"""SELECT TRIM(sire),
    COALESCE(ROUND(SUM(CASE WHEN race_name LIKE '%新馬%' AND finish=1 THEN odds ELSE 0 END)*100.0/
      NULLIF(SUM(CASE WHEN race_name LIKE '%新馬%' THEN 1 ELSE 0 END),0), 0), 0),
    COALESCE(ROUND(SUM(CASE WHEN race_name LIKE '%未勝利%' AND finish=1 THEN odds ELSE 0 END)*100.0/
      NULLIF(SUM(CASE WHEN race_name LIKE '%未勝利%' THEN 1 ELSE 0 END),0), 0), 0)
FROM results WHERE {BASE}
GROUP BY 1 HAVING SUM(CASE WHEN race_name LIKE '%新馬%' THEN 1 ELSE 0 END) >= 30
  AND SUM(CASE WHEN race_name LIKE '%未勝利%' THEN 1 ELSE 0 END) >= 100
ORDER BY (COALESCE(ROUND(SUM(CASE WHEN race_name LIKE '%未勝利%' AND finish=1 THEN odds ELSE 0 END)*100.0/
  NULLIF(SUM(CASE WHEN race_name LIKE '%未勝利%' THEN 1 ELSE 0 END),0),0),0) -
  COALESCE(ROUND(SUM(CASE WHEN race_name LIKE '%新馬%' AND finish=1 THEN odds ELSE 0 END)*100.0/
  NULLIF(SUM(CASE WHEN race_name LIKE '%新馬%' THEN 1 ELSE 0 END),0),0),0)) DESC LIMIT 12""")
sections.append(('kakusei', '2走目で覚醒する「遅咲き血統」',
    '新馬戦では走らないが未勝利戦で急激に成績が上がる種牡馬。',
    [{'name':r[0],'shinba':r[1],'mikatsuri':r[2]} for r in rows]))

# 7. 距離延長
rows = q(f"""SELECT TRIM(sire),
    COALESCE(ROUND(SUM(CASE WHEN distance > prev_distance AND finish=1 THEN odds ELSE 0 END)*100.0/
      NULLIF(SUM(CASE WHEN distance > prev_distance THEN 1 ELSE 0 END),0), 0), 0),
    COALESCE(ROUND(SUM(CASE WHEN distance < prev_distance AND finish=1 THEN odds ELSE 0 END)*100.0/
      NULLIF(SUM(CASE WHEN distance < prev_distance THEN 1 ELSE 0 END),0), 0), 0)
FROM results WHERE {BASE} AND prev_distance > 0
GROUP BY 1 HAVING SUM(CASE WHEN distance > prev_distance THEN 1 ELSE 0 END) >= 100
  AND SUM(CASE WHEN distance < prev_distance THEN 1 ELSE 0 END) >= 100
ORDER BY (COALESCE(ROUND(SUM(CASE WHEN distance > prev_distance AND finish=1 THEN odds ELSE 0 END)*100.0/
  NULLIF(SUM(CASE WHEN distance > prev_distance THEN 1 ELSE 0 END),0),0),0)) DESC LIMIT 12""")
sections.append(('extension', '距離延長で化ける種牡馬',
    '前走より距離が伸びたときに好走する「スタミナ開花型」の血統。',
    [{'name':r[0],'ext':r[1],'shr':r[2]} for r in rows]))

# 8. 季節別
rows = q(f"""SELECT TRIM(sire),
    COALESCE(ROUND(SUM(CASE WHEN CAST(substr(date,6,2) AS INT) BETWEEN 3 AND 5 AND finish=1 THEN odds ELSE 0 END)*100.0/
      NULLIF(SUM(CASE WHEN CAST(substr(date,6,2) AS INT) BETWEEN 3 AND 5 THEN 1 ELSE 0 END),0), 0), 0),
    COALESCE(ROUND(SUM(CASE WHEN CAST(substr(date,6,2) AS INT) BETWEEN 6 AND 8 AND finish=1 THEN odds ELSE 0 END)*100.0/
      NULLIF(SUM(CASE WHEN CAST(substr(date,6,2) AS INT) BETWEEN 6 AND 8 THEN 1 ELSE 0 END),0), 0), 0),
    COALESCE(ROUND(SUM(CASE WHEN CAST(substr(date,6,2) AS INT) BETWEEN 9 AND 11 AND finish=1 THEN odds ELSE 0 END)*100.0/
      NULLIF(SUM(CASE WHEN CAST(substr(date,6,2) AS INT) BETWEEN 9 AND 11 THEN 1 ELSE 0 END),0), 0), 0),
    COALESCE(ROUND(SUM(CASE WHEN (CAST(substr(date,6,2) AS INT)=12 OR CAST(substr(date,6,2) AS INT)<=2) AND finish=1 THEN odds ELSE 0 END)*100.0/
      NULLIF(SUM(CASE WHEN CAST(substr(date,6,2) AS INT)=12 OR CAST(substr(date,6,2) AS INT)<=2 THEN 1 ELSE 0 END),0), 0), 0),
    COUNT(*)
FROM results WHERE {BASE}
GROUP BY 1 HAVING COUNT(*) >= 500
ORDER BY 2 DESC LIMIT 12""")
sections.append(('season', '季節で成績が変わる種牡馬',
    '春夏秋冬で回収率に大きな差がある種牡馬。季節の変わり目に注目。',
    [{'name':r[0],'spring':r[1],'summer':r[2],'autumn':r[3],'winter':r[4],'n':r[5]} for r in rows]))

conn.close()

# Generate HTML
def roi_color(roi):
    if roi >= 200: return '#D32F2F'
    if roi >= 150: return '#E65100'
    if roi >= 100: return '#2E7D32'
    return '#757575'

def roi_bar(roi, max_roi=400):
    w = min(100, roi / max_roi * 100)
    c = roi_color(roi)
    return f'<div style="display:flex;align-items:center;gap:6px"><div style="width:{w}%;height:8px;background:{c};border-radius:4px;min-width:4px"></div><span style="font-weight:700;color:{c};font-size:13px">{roi:.0f}%</span></div>'

html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NORISHICO AI — 血統データラボ</title>
<style>
:root{{--bg:#FAF7F2;--card:#FFF;--border:#E8DDD0;--text:#2D2A26;--sub:#8B7E6E;--orange:#F28C28;--green:#4A7C59;--red:#D32F2F}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);max-width:720px;margin:0 auto}}
.header{{background:linear-gradient(135deg,#2D2A26,#4A3728);color:white;padding:24px 20px;text-align:center}}
.header h1{{font-size:18px;letter-spacing:2px}}
.header p{{font-size:11px;color:#BDA88E;margin-top:4px}}
.nav{{display:flex;overflow-x:auto;gap:0;background:#FFF;border-bottom:2px solid var(--border);position:sticky;top:0;z-index:10}}
.nav-item{{padding:12px 16px;font-size:12px;font-weight:600;color:var(--sub);cursor:pointer;white-space:nowrap;border-bottom:3px solid transparent;transition:.2s}}
.nav-item:hover,.nav-item.active{{color:var(--orange);border-bottom-color:var(--orange)}}
.section{{display:none;padding:16px}}
.section.active{{display:block}}
.section-header{{margin-bottom:12px}}
.section-header h2{{font-size:16px;color:var(--text)}}
.section-header p{{font-size:11px;color:var(--sub);margin-top:4px;line-height:1.6}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:12px;margin-bottom:10px;overflow:hidden}}
.card-row{{display:flex;align-items:center;padding:10px 14px;border-bottom:1px solid #F5EDE5;gap:10px}}
.card-row:last-child{{border-bottom:none}}
.rank{{font-size:18px;font-weight:800;color:var(--orange);min-width:28px;text-align:center}}
.rank.gold{{color:#FFB300}}.rank.silver{{color:#90A4AE}}.rank.bronze{{color:#A1887F}}
.name{{font-weight:700;font-size:13px;flex:1;min-width:0}}
.name small{{font-weight:400;color:var(--sub);font-size:10px;display:block}}
.stat{{text-align:right;min-width:60px}}
.stat .value{{font-size:15px;font-weight:700}}.stat .label{{font-size:9px;color:var(--sub)}}
.tag{{display:inline-block;font-size:9px;padding:2px 8px;border-radius:8px;font-weight:600}}
.tag-hot{{background:#FFF3E0;color:#E65100}}.tag-ok{{background:#E8F5E9;color:#2E7D32}}.tag-cold{{background:#F5F5F5;color:#757575}}
.vs-row{{display:grid;grid-template-columns:1fr auto 1fr;gap:8px;padding:10px 14px;border-bottom:1px solid #F5EDE5;align-items:center}}
.vs-label{{font-size:10px;color:var(--sub);text-align:center}}
.bar-section{{padding:10px 14px;border-bottom:1px solid #F5EDE5}}
.bar-label{{font-size:12px;font-weight:600;margin-bottom:4px}}
.season-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;padding:10px 14px}}
.season-cell{{text-align:center;padding:8px 4px;border-radius:8px;font-size:11px}}
.season-cell .szn{{font-weight:700;font-size:10px;color:var(--sub)}}
.season-cell .val{{font-size:14px;font-weight:700;margin-top:2px}}
.disclaimer{{padding:16px;font-size:10px;color:var(--sub);text-align:center;line-height:1.6}}
</style>
</head>
<body>
<div class="header">
<h1>NORISHICO AI — BLOOD DATA LAB</h1>
<p>keiba.db 2019-2026 / 335,700頭のデータから導出</p>
</div>
<div class="nav" id="nav">
"""

tab_names = [('nicks','ニックス'),('dam_sire','母父力'),('himba','牝馬限定'),('shinba','新馬戦'),
             ('heavy','重馬場'),('kakusei','覚醒型'),('extension','距離延長'),('season','季節別')]

for i, (key, label) in enumerate(tab_names):
    act = ' active' if i == 0 else ''
    html += f'<div class="nav-item{act}" onclick="switchTab(\'{key}\')">{label}</div>\n'
html += '</div>\n'

# Generate each section
for idx, (key, title, desc, items) in enumerate(sections):
    act = ' active' if idx == 0 else ''
    html += f'<div class="section{act}" id="sec-{key}">\n'
    html += f'<div class="section-header"><h2>{title}</h2><p>{desc}</p></div>\n'
    html += '<div class="card">\n'

    if key == 'nicks':
        for i, item in enumerate(items):
            rank_cls = ['gold','silver','bronze'][i] if i < 3 else ''
            tag = 'tag-hot' if item['roi'] >= 200 else ('tag-ok' if item['roi'] >= 100 else 'tag-cold')
            html += f'<div class="card-row"><span class="rank {rank_cls}">{i+1}</span>'
            html += f'<div class="name">{item["sire"]}<small>× 母父{item["dam_sire"]}</small></div>'
            html += f'<div class="stat"><div class="value" style="color:{roi_color(item["roi"])}">{item["roi"]:.0f}%</div><div class="label">単回 n={item["n"]}</div></div></div>\n'

    elif key == 'dam_sire':
        for i, item in enumerate(items):
            rank_cls = ['gold','silver','bronze'][i] if i < 3 else ''
            html += f'<div class="card-row"><span class="rank {rank_cls}">{i+1}</span>'
            html += f'<div class="name">母父 {item["name"]}</div>'
            html += f'<div class="stat"><div class="value" style="color:{roi_color(item["roi"])}">{item["roi"]:.0f}%</div><div class="label">n={item["n"]}</div></div></div>\n'

    elif key in ('himba', 'shinba'):
        for i, item in enumerate(items):
            rank_cls = ['gold','silver','bronze'][i] if i < 3 else ''
            html += f'<div class="card-row"><span class="rank {rank_cls}">{i+1}</span>'
            html += f'<div class="name">{item["name"]}</div>'
            html += f'<div class="stat"><div class="value" style="color:{roi_color(item["roi"])}">{item["roi"]:.0f}%</div><div class="label">n={item["n"]}</div></div></div>\n'

    elif key == 'heavy':
        for i, item in enumerate(items):
            tc = roi_color(item['turf']); dc = roi_color(item['dirt'])
            html += f'<div class="card-row" style="display:block;padding:10px 14px">'
            html += f'<div style="font-weight:700;font-size:13px;margin-bottom:6px">{item["name"]} <span style="font-size:10px;color:var(--sub)">n={item["n"]}</span></div>'
            html += f'<div style="display:flex;gap:12px">'
            html += f'<div style="flex:1"><div style="font-size:10px;color:var(--sub)">重・芝</div><div style="font-size:16px;font-weight:700;color:{tc}">{item["turf"]:.0f}%</div></div>'
            html += f'<div style="flex:1"><div style="font-size:10px;color:var(--sub)">重・ダ</div><div style="font-size:16px;font-weight:700;color:{dc}">{item["dirt"]:.0f}%</div></div>'
            html += f'</div></div>\n'

    elif key == 'kakusei':
        for i, item in enumerate(items):
            diff = item['mikatsuri'] - item['shinba']
            html += f'<div class="card-row" style="display:block;padding:10px 14px">'
            html += f'<div style="font-weight:700;font-size:13px;margin-bottom:4px">{item["name"]} <span class="tag tag-hot">+{diff:.0f}pt覚醒</span></div>'
            html += f'<div style="display:flex;gap:16px;font-size:12px">'
            html += f'<div>新馬 <span style="font-weight:700;color:{roi_color(item["shinba"])}">{item["shinba"]:.0f}%</span></div>'
            html += f'<div>→ 未勝利 <span style="font-weight:700;color:{roi_color(item["mikatsuri"])}">{item["mikatsuri"]:.0f}%</span></div>'
            html += f'</div></div>\n'

    elif key == 'extension':
        for i, item in enumerate(items):
            html += f'<div class="card-row" style="display:block;padding:10px 14px">'
            html += f'<div style="font-weight:700;font-size:13px;margin-bottom:4px">{item["name"]}</div>'
            html += f'<div style="display:flex;gap:16px;font-size:12px">'
            html += f'<div>延長 <span style="font-weight:700;color:{roi_color(item["ext"])}">{item["ext"]:.0f}%</span></div>'
            html += f'<div>短縮 <span style="font-weight:700;color:{roi_color(item["shr"])}">{item["shr"]:.0f}%</span></div>'
            html += f'</div></div>\n'

    elif key == 'season':
        for i, item in enumerate(items):
            vals = [('春',item['spring']),('夏',item['summer']),('秋',item['autumn']),('冬',item['winter'])]
            html += f'<div style="padding:10px 14px;border-bottom:1px solid #F5EDE5">'
            html += f'<div style="font-weight:700;font-size:13px;margin-bottom:6px">{item["name"]} <span style="font-size:10px;color:var(--sub)">n={item["n"]}</span></div>'
            html += f'<div class="season-grid">'
            for szn, val in vals:
                bg = '#FFF3E0' if val >= 100 else '#F5F5F5'
                html += f'<div class="season-cell" style="background:{bg}"><div class="szn">{szn}</div><div class="val" style="color:{roi_color(val)}">{val:.0f}%</div></div>'
            html += '</div></div>\n'

    html += '</div>\n</div>\n'

html += """
<div class="disclaimer">
⚠️ 過去の統計データであり将来の結果を保証するものではありません<br>
NORISHICO AI — keiba.db 2019-2026
</div>
<script>
function switchTab(key) {
  document.querySelectorAll('.nav-item').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('sec-' + key).classList.add('active');
}
</script>
</body></html>"""

with open('docs/blood_data_lab.html', 'w', encoding='utf-8') as f:
    f.write(html)
print(f'Generated: docs/blood_data_lab.html ({len(html):,} bytes)')
print(f'Sections: {len(sections)}')
