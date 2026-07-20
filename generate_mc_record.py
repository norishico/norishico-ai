"""
generate_mc_record.py — 今日のMC予想HTML生成
N_MC=40 / seed固定 / 3馬場パターン（良稍重/重/不良）
python generate_mc_record.py [YYYY-MM-DD]
"""
import sqlite3, sys, json, re
import numpy as np
from pathlib import Path
from sim_bt_full import load_horse_hist, get_horse_history_fast, umaban_to_gate
from backtest_sim_lite import STYLE_DEF
import generate_race_sim as gsim

sys.stdout.reconfigure(encoding='utf-8')

from datetime import date as _date
TARGET_DATE = sys.argv[1] if len(sys.argv) > 1 else _date.today().isoformat()
N_MC  = 1000
SEED  = 42
DB    = 'keiba.db'
HTML  = 'mc_record.html'

VENUE_CODE = {'函館':2,'札幌':1,'福島':4,'新潟':5,'東京':5,'中山':6,
              '中京':9,'京都':8,'阪神':9,'小倉':10}


def load_start_time_map():
    """this_week_races.jsonからveune×race_num→{start_time, nk_id}のマップを返す"""
    p = Path('this_week_races.json')
    if not p.exists():
        return {}
    races = json.load(open(p, encoding='utf-8'))
    return {(r['venue'], r['race_num']): {
        'start_time': r.get('start_time', ''),
        'nk_id': r.get('race_id', ''),
    } for r in races if r.get('date') == TARGET_DATE}


def get_gate_bias(conn, venue, surface, distance):
    """venue×surface×distance(±300m)の枠番別複勝率 {gate: rate}"""
    rows = conn.execute('''
        SELECT
          CASE WHEN umaban<=2 THEN 1 WHEN umaban<=4 THEN 2 WHEN umaban<=6 THEN 3
               WHEN umaban<=8 THEN 4 WHEN umaban<=10 THEN 5 WHEN umaban<=12 THEN 6
               WHEN umaban<=14 THEN 7 ELSE 8 END as gate,
          COUNT(*) as n,
          SUM(CASE WHEN finish<=3 THEN 1.0 ELSE 0 END) as top3
        FROM results
        WHERE venue=? AND surface=? AND distance BETWEEN ? AND ?
          AND finish IS NOT NULL AND finish>0 AND umaban IS NOT NULL
        GROUP BY gate HAVING COUNT(*)>=30
    ''', (venue, surface, distance - 300, distance + 300)).fetchall()
    if len(rows) < 4:  # データ不足 → venue全距離にフォールバック
        rows = conn.execute('''
            SELECT
              CASE WHEN umaban<=2 THEN 1 WHEN umaban<=4 THEN 2 WHEN umaban<=6 THEN 3
                   WHEN umaban<=8 THEN 4 WHEN umaban<=10 THEN 5 WHEN umaban<=12 THEN 6
                   WHEN umaban<=14 THEN 7 ELSE 8 END as gate,
              COUNT(*) as n,
              SUM(CASE WHEN finish<=3 THEN 1.0 ELSE 0 END) as top3
            FROM results
            WHERE venue=? AND surface=?
              AND finish IS NOT NULL AND finish>0 AND umaban IS NOT NULL
            GROUP BY gate HAVING COUNT(*)>=50
        ''', (venue, surface)).fetchall()
    return {gate: top3 / n for gate, n, top3 in rows}


def run_mc_3pat(horses, n_mc, seed_base, gate_bias=None):
    if gate_bias is None:
        gate_bias = {}
    results = {}
    for pat_i, (label, track_cond) in enumerate([
        ('良・稍重','良'), ('重','重'), ('不良','不良')
    ]):
        rng = np.random.default_rng(seed_base * 10 + pat_i)
        race_info = {
            'venue': horses[0].get('venue',''),
            'distance': horses[0].get('distance', 1600),
            'track_cond': track_cond,
            'num_horses': len(horses)
        }
        rates = run_mc_fixed(horses, race_info, n_mc, rng)
        ranked = sorted(range(len(rates)), key=lambda i: (
            -rates[i],
            -gate_bias.get(horses[i].get('gate', 9), 0.0)
        ))

        jiku_i = ranked[0]
        jiku_h = horses[jiku_i]
        aite_hs = [horses[ranked[k]] for k in range(1, 4) if ranked[k] < len(horses)]

        results[label] = {
            'jiku': {
                'no':     jiku_h['umaban'],
                'name':   jiku_h['horse_name'],
                'style':  jiku_h['style'],
                'jockey': jiku_h.get('jockey', ''),
                'mc':     round(rates[jiku_i], 2),
            },
            'aite': [{
                'no':     h['umaban'],
                'name':   h['horse_name'],
                'style':  h['style'],
                'jockey': h.get('jockey', ''),
                'mc':     round(rates[ranked[k+1]], 2),
            } for k, h in enumerate(aite_hs)]
        }
    return results


def run_mc_fixed(horses, race_info, n_mc, rng):
    n = len(horses)
    if n < 3:
        return [1.0/n]*n
    top3 = np.zeros(n)
    tc = race_info.get('track_cond','良')
    venue = race_info.get('venue','')
    dist  = race_info.get('distance', 1600)
    # 東京2600m/中山3390m/中山3110mはDB照合の結果、該当レース0件（JRAに実在しない距離）のため削除(2026-07-20)
    COURSE_ADV = {'阪神':{1800:3},'中京':{1800:3,2000:3}}
    c_adj  = COURSE_ADV.get(venue,{}).get(dist,0)
    heavy  = tc in ('重','不良')
    hv     = 3.0 if tc=='不良' else 2.0
    n_nige = sum(1 for h in horses if h['style']=='逃げ')
    n_front= sum(1 for h in horses if h['style'] in ('逃げ','先行'))
    num_h  = race_info.get('num_horses', n)
    pH,pM,pS = 0.25,0.40,0.35
    if n_nige>=3:
        adj=min(0.20,0.08*(n_nige-1)); pH=min(0.55,pH+adj); pS=max(0.10,pS-adj*0.7); pM=max(0.20,pM-adj*0.3)
    elif n_nige==2: pH=min(0.50,pH+0.08); pS=max(0.15,pS-0.05)
    elif n_nige==0: pH=max(0.05,pH-0.10); pS=min(0.55,pS+0.10)
    if n>0 and n_front/n>0.50: pH=min(0.50,pH+0.05); pS=max(0.10,pS-0.05)
    if num_h>=17: pH=min(0.55,pH+0.12); pS=max(0.10,pS-0.09)
    elif num_h>=13: pH=min(0.50,pH+0.08); pS=max(0.10,pS-0.06)
    elif num_h<=8:  pH=max(0.05,pH-0.05); pS=min(0.55,pS+0.04)
    inner = any(h.get('gate',5)<=4 and h['style'] in ('逃げ','先行') for h in horses)
    outer = any(h.get('gate',5)>=5 and h['style'] in ('逃げ','先行') for h in horses)
    if inner and outer: pH=min(0.55,pH+0.03); pS=max(0.10,pS-0.03)
    t = pH+pM+pS; pH/=t; pM/=t; pS/=t
    for _ in range(n_mc):
        P = rng.choice(['H','M','S'], p=[pH,pM,pS])
        gain = np.zeros(n)
        for i,h in enumerate(horses):
            st  = h['style']
            pac = STYLE_DEF.get(st, STYLE_DEF['先行'])
            v   = pac.get(P, 60)
            l3f = h.get('last3f') or 34.5
            g   = (75.0*v/100-70)*0.45 + (34.0-l3f)*1.5
            bon = 0.0
            if P=='S':
                if st=='逃げ': bon=2.0
                elif st=='先行': bon=0.5
            if heavy:
                if st=='逃げ': bon+=hv
                elif st=='先行': bon+=hv*0.6
                elif st in ('差し','追い込み'): bon-=hv*0.5
            if c_adj>0 and st in ('逃げ','先行'): bon+=c_adj*0.5
            elif c_adj<0 and st in ('差し','追い込み'): bon+=abs(c_adj)*0.5
            gate=h.get('gate',4)
            if gate>=6 and st in ('逃げ','先行'):
                if any(hh.get('gate',4)<=4 and hh['style'] in ('逃げ','先行') for j2,hh in enumerate(horses) if j2!=i):
                    if P=='H': g-=1.0
            gain[i]=g+bon
        noise = rng.normal(0,5,n)
        order = np.argsort(-(gain+noise))
        for pos in range(min(3,n)): top3[order[pos]]+=1
    return (top3/n_mc).tolist()


def build_races(conn, horse_hist):
    st_map = load_start_time_map()
    print(f'発走時刻マップ: {len(st_map)}件 (this_week_races.json)')

    # this_week_races.jsonから当日出走馬を取得（出走前でも使えるように）
    p = Path('this_week_races.json')
    all_json = json.load(open(p, encoding='utf-8')) if p.exists() else []
    json_races = [r for r in all_json
                  if r.get('date') == TARGET_DATE and r.get('surface') == 'ダ']

    def is_valid_race(r):
        rname = r.get('race_name') or ''
        if '新馬' in rname or '未勝利' in rname:
            return False
        ages = []
        for h in r.get('horses', []):
            m = re.search(r'\d+', h.get('age', '') or '')
            if m:
                ages.append(int(m.group()))
        max_age = max(ages) if ages else 0
        if not rname and max_age < 4:
            return False
        return True

    json_races = [r for r in json_races if is_valid_race(r)]
    print(f'新馬・未勝利除外後: {len(json_races)}件')

    races_js = []
    for race_idx, race in enumerate(sorted(json_races, key=lambda r: (r.get('venue', ''), r.get('race_num', 0)))):
        venue  = race.get('venue', '')
        rno    = race.get('race_num', 0)
        dst    = race.get('distance') or 1600
        srf    = race.get('surface', 'ダ')
        rname  = race.get('race_name') or f'{dst}mダート'
        horses_raw = race.get('horses', [])

        horses = []
        for hi, h in enumerate(horses_raw):
            hn  = (h.get('name', '') or '').strip()
            uma = h.get('umaban') or (hi + 1)
            jk  = (h.get('jockey', '') or '').strip()
            hist   = get_horse_history_fast(hn, TARGET_DATE, srf, horse_hist)
            last3f = next((hh['last3f'] for hh in hist if hh.get('last3f')), None)
            style  = gsim.classify_style(hist, dst or 1600, jockey=jk, surface=srf)
            horses.append({
                'horse_name': hn, 'umaban': uma,
                'jockey': jk, 'last3f': last3f,
                'style': style, 'gate': umaban_to_gate(uma),
                'venue': venue, 'distance': dst or 1600,
                'pop': int(h.get('popularity', 0) or 0) if str(h.get('popularity', '0')).isdigit() else 0,
                'odds': float(h.get('odds', 0.0) or 0.0) if str(h.get('odds', '0')).replace('.', '').isdigit() else 0.0,
            })

        if len(horses) < 4:
            continue

        gate_bias = get_gate_bias(conn, venue, srf or 'ダ', dst or 1600)
        patterns  = run_mc_3pat(horses, N_MC, SEED * 1000 + race_idx, gate_bias=gate_bias)

        rec_id = f"{TARGET_DATE.replace('-', '')}_{venue}_{rno}"
        info   = st_map.get((venue, rno), {})
        st     = info.get('start_time') or race.get('start_time') or f'{10 + rno // 2}:00'
        nk_id  = info.get('nk_id', '') or race.get('race_id', '')

        races_js.append({
            'id':         rec_id,
            'date':       TARGET_DATE,
            'venue':      venue,
            'rno':        rno,
            'rname':      rname,
            'dst':        dst,
            'cnt':        len(horses),
            'start_time': st,
            'nk_id':      nk_id,
            'patterns':   patterns,
        })

    return races_js


def races_to_js(races):
    lines = ['const RACES = [']
    for r in races:
        p = r['patterns']
        def pat_str(label):
            pat = p[label]
            j = pat['jiku']
            aite_str = ',\n          '.join(
                f'{{ no:{a["no"]}, name:"{a["name"]}", style:"{a["style"]}", jockey:"{a["jockey"]}", mc:{a["mc"]} }}'
                for a in pat['aite']
            )
            return (
                f'      "{label}": {{\n'
                f'        jiku: {{ no:{j["no"]}, name:"{j["name"]}", style:"{j["style"]}", jockey:"{j["jockey"]}", mc:{j["mc"]} }},\n'
                f'        aite: [\n          {aite_str}\n        ]\n'
                f'      }}'
            )
        pats = ',\n'.join(pat_str(lb) for lb in ['良・稍重','重','不良'])
        lines.append(
            f'  {{\n'
            f'    id: "{r["id"]}", date: "{r["date"]}", venue: "{r["venue"]}", rno: {r["rno"]},\n'
            f'    rname: "{r["rname"]}", dst: {r["dst"]}, cnt: {r["cnt"]},\n'
            f'    start_time: "{r["start_time"]}", nk_id: "{r["nk_id"]}",\n'
            f'    patterns: {{\n{pats}\n    }}\n'
            f'  }},'
        )
    lines.append('];')
    return '\n'.join(lines)


def write_widget_json(races):
    """docs/widget_data.json を書き出す (GitHub Pages経由でScriptableウィジェットが取得)"""
    out = []
    for r in races:
        pat = r['patterns'].get('良・稍重', {})
        jiku = pat.get('jiku', {})
        aite = pat.get('aite', [])
        out.append({
            'venue':      r['venue'],
            'rno':        r['rno'],
            'rname':      r['rname'],
            'start_time': r['start_time'],
            'nk_id':      r['nk_id'],
            'jiku_no':    jiku.get('no', 0),
            'jiku_name':  jiku.get('name', ''),
            'aite':       [a['name'] for a in aite[:3]],
        })
    data = {'date': TARGET_DATE, 'races': out}
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    docs = Path('docs')
    docs.mkdir(exist_ok=True)
    (docs / 'widget_data.json').write_text(payload, encoding='utf-8')
    pub = Path('mc_keiba_public')
    if pub.exists():
        (pub / 'widget_data.json').write_text(payload, encoding='utf-8')
    print(f'widget_data.json書き出し完了: {len(out)}件')


def load_backup_records():
    """records_backup.jsonを読んでdictを返す（なければ空dict）"""
    p = Path('records_backup.json')
    if not p.exists():
        return {}
    try:
        data = json.load(open(p, encoding='utf-8'))
        recs = data.get('records', data) if isinstance(data, dict) else {}
        print(f'records_backup.json読み込み: {len(recs)}件')
        return recs
    except Exception as e:
        print(f'records_backup.json読み込みエラー: {e}')
        return {}


def update_html(races_js_str):
    html = Path(HTML).read_text(encoding='utf-8')
    new_html = re.sub(
        r'const RACES = \[.*?\];',
        races_js_str,
        html,
        flags=re.DOTALL
    )
    # records_backup.jsonがあればRECOVERY_BACKUPを自動更新
    backup = load_backup_records()
    backup_js = f'const RECOVERY_BACKUP = {json.dumps(backup, ensure_ascii=False)};'
    new_html = re.sub(r'const RECOVERY_BACKUP = \{.*?\};', backup_js, new_html)
    Path(HTML).write_text(new_html, encoding='utf-8')
    print(f'HTML更新完了: {HTML}')
    pub = Path('mc_keiba_public/index.html')
    pub.write_text(new_html, encoding='utf-8')
    print(f'公開用コピー: {pub}')


def main():
    conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    conn.execute('PRAGMA cache_size=-65536')
    print('horse_hist読み込み中...')
    horse_hist = load_horse_hist(conn)
    print(f'完了。{TARGET_DATE}のレースMC計算中...')
    races = build_races(conn, horse_hist)
    print(f'{len(races)}件のレース計算完了')
    js_str = races_to_js(races)
    update_html(js_str)
    write_widget_json(races)
    conn.close()
    nk_empty = [r['id'] for r in races if not r.get('nk_id')]
    if nk_empty:
        print(f'nk_id未設定: {nk_empty}')
    else:
        print(f'nk_id全件設定済み')


if __name__ == '__main__':
    main()
