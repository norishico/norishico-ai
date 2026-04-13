"""案B: オッズ急騰(前走比200%+)を除外した場合のROI"""
import json, sqlite3
from collections import defaultdict

conn = sqlite3.connect('keiba.db')
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA cache_size=-65536")
conn.execute("PRAGMA temp_store=MEMORY")

# 全年のbet_recordsから、v6本体の前走オッズを取得して除外シミュレーション
print("=" * 80)
print("案B: オッズ急騰(前走比200%+)除外シミュレーション")
print("=" * 80)

for threshold, label in [(2.0, '200%+除外'), (1.5, '150%+除外'), (3.0, '300%+除外')]:
    print(f"\n--- {label} (ratio >= {threshold}) ---")
    yearly_orig = defaultdict(lambda: [0, 0, 0])  # n, inv, ret
    yearly_filt = defaultdict(lambda: [0, 0, 0])
    yearly_cut = defaultdict(lambda: [0, 0, 0])

    for year in range(2020, 2027):
        d = json.load(open(f'btv6_{year}.json', encoding='utf-8'))
        for b in d['bet_records']:
            if b.get('special'): continue
            yr = year
            cost = b.get('cost', 1000)
            ret = b.get('ret', 0)
            yearly_orig[yr][0] += 1; yearly_orig[yr][1] += cost; yearly_orig[yr][2] += ret

            # 前走オッズ取得
            prev = conn.execute("""
                SELECT odds FROM results
                WHERE TRIM(horse_name) = ? AND date < ? AND odds > 0 AND finish < 90
                ORDER BY date DESC LIMIT 1
            """, (b['honmei_name'], b['date'])).fetchone()

            if prev:
                odds = b.get('honmei_odds') or 0
                ratio = odds / prev['odds'] if prev['odds'] > 0 else 1.0
                if ratio >= threshold:
                    yearly_cut[yr][0] += 1; yearly_cut[yr][1] += cost; yearly_cut[yr][2] += ret
                    continue

            yearly_filt[yr][0] += 1; yearly_filt[yr][1] += cost; yearly_filt[yr][2] += ret

    print(f"  {'年':>6} {'元R':>4} {'元ROI':>7} | {'除外':>3} {'除外ROI':>7} | {'残R':>4} {'残ROI':>7} | {'差':>6}")
    print("  " + "-" * 65)
    to = [0,0,0]; tf = [0,0,0]; tc = [0,0,0]
    for yr in range(2020, 2027):
        o = yearly_orig[yr]; f = yearly_filt[yr]; c = yearly_cut[yr]
        o_roi = o[2]/o[1]*100 if o[1] else 0
        f_roi = f[2]/f[1]*100 if f[1] else 0
        c_roi = c[2]/c[1]*100 if c[1] else 0
        diff = f_roi - o_roi
        print(f"  {yr:>6} {o[0]:>4} {o_roi:>6.1f}% | {c[0]:>3} {c_roi:>6.1f}% | {f[0]:>4} {f_roi:>6.1f}% | {diff:>+5.1f}pt")
        for i in range(3): to[i]+=o[i]; tf[i]+=f[i]; tc[i]+=c[i]
    print("  " + "-" * 65)
    o_roi = to[2]/to[1]*100; f_roi = tf[2]/tf[1]*100; c_roi = tc[2]/tc[1]*100 if tc[1] else 0
    print(f"  {'合計':>6} {to[0]:>4} {o_roi:>6.1f}% | {tc[0]:>3} {c_roi:>6.1f}% | {tf[0]:>4} {f_roi:>6.1f}% | {f_roi-o_roi:>+5.1f}pt")
    print(f"  v6損益: {to[2]-to[1]:>+,} → {tf[2]-tf[1]:>+,} (差{tf[2]-tf[1]-to[2]+to[1]:>+,})")
