"""
netkeiba血統スクレイパ (JV-Link蓄積系の代替)
db.netkeiba.com/horse/ajax_horse_pedigree.html?id={horse_id} を叩いて
sire/dam/dam_sire/sire_id/dam_id を取得、キャッシュ+DB UPDATE。

使い方:
    python scripts/fetch_blood_netkeiba.py           # NULL血統の全horse_idを処理(デフォルトstaging)
    python scripts/fetch_blood_netkeiba.py --prod    # 本番keiba.dbを対象
    python scripts/fetch_blood_netkeiba.py --limit 5 # 5頭だけテスト実行
"""
import argparse, json, pathlib, re, sqlite3, sys, time, urllib.request, datetime

PROJ = pathlib.Path(__file__).resolve().parent.parent
CACHE_PATH = PROJ / 'blood_cache.json'
LOG_PATH = PROJ / 'logs' / f'blood_fetch_{datetime.datetime.now():%Y%m%d_%H%M%S}.log'
AJAX_URL = 'https://db.netkeiba.com/horse/ajax_horse_pedigree.html'
UA = 'Mozilla/5.0 (compatible; NorishikoAI/1.0)'
SLEEP = 1.0  # 秒/req (レートリミット配慮)
A_TAG_RE = re.compile(r'<a href="https://db\.netkeiba\.com/horse/ped/([^/]+)/"[^>]*>\s*<span[^>]*>([^<]+)</span>\s*</a>', re.DOTALL)

def log(msg):
    print(msg, flush=True)
    with open(LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(msg + '\n')

def load_cache():
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}

def save_cache(cache):
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding='utf-8')

def fetch_blood(horse_id, timeout=15):
    """Return dict with sire/dam/dam_sire/sire_id/dam_id or None on failure."""
    url = f'{AJAX_URL}?input=UTF-8&output=json&id={horse_id}'
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Referer': f'https://db.netkeiba.com/horse/{horse_id}/',
    })
    raw = urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', errors='replace')
    obj = json.loads(raw)
    if obj.get('status') != 'OK':
        return None
    inner = obj.get('data', '')
    # extract a-tag sequences (id, name) in blood_table
    # Note: inner is escaped JSON so we work on the raw string as-is
    found = A_TAG_RE.findall(inner)
    if len(found) < 5:
        return None
    # order: 0=sire, 1=sire-sire, 2=sire-dam, 3=dam, 4=dam-sire, 5=dam-dam
    sire_id, sire = found[0]
    dam_id, dam = found[3]
    _, dam_sire = found[4]
    return {
        'sire': sire.strip(),
        'dam': dam.strip(),
        'dam_sire': dam_sire.strip(),
        'sire_id': sire_id.strip(),
        'dam_id': dam_id.strip(),
    }

def target_db(args):
    return PROJ / ('keiba.db' if args.prod else 'keiba_staging.db')

def list_missing(db_path, limit=None):
    conn = sqlite3.connect(db_path)
    q = "SELECT DISTINCT horse_id FROM results WHERE sire IS NULL AND horse_id IS NOT NULL AND horse_id != ''"
    if limit:
        q += f' LIMIT {int(limit)}'
    rows = [r[0] for r in conn.execute(q).fetchall()]
    conn.close()
    return rows

def apply_to_db(db_path, cache):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    updated = 0
    for hid, b in cache.items():
        if not b:
            continue
        r = cur.execute(
            "UPDATE results SET sire=?, dam=?, dam_sire=?, sire_id=?, dam_id=? "
            "WHERE horse_id=? AND sire IS NULL",
            (b['sire'], b['dam'], b['dam_sire'], b['sire_id'], b['dam_id'], hid),
        )
        updated += r.rowcount
    conn.commit()
    conn.close()
    return updated

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--prod', action='store_true', help='Target keiba.db instead of staging')
    ap.add_argument('--limit', type=int, default=None, help='Process only N horses (testing)')
    ap.add_argument('--skip-fetch', action='store_true', help='Skip fetching, only apply cached blood to DB')
    args = ap.parse_args()

    LOG_PATH.parent.mkdir(exist_ok=True)
    db_path = target_db(args)
    log(f'[blood] start {datetime.datetime.now().isoformat()} target={db_path.name}')

    cache = load_cache()
    log(f'  cache: {len(cache)} entries loaded')

    if not args.skip_fetch:
        targets = list_missing(db_path, args.limit)
        log(f'  targets: {len(targets)} unique horse_ids with NULL sire')
        new_count = 0
        fail_count = 0
        for i, hid in enumerate(targets, 1):
            if hid in cache:
                continue
            try:
                b = fetch_blood(hid)
                cache[hid] = b
                if b:
                    new_count += 1
                else:
                    fail_count += 1
                    log(f'    [{i}/{len(targets)}] {hid}: parse fail')
            except Exception as e:
                fail_count += 1
                cache[hid] = None
                log(f'    [{i}/{len(targets)}] {hid}: {e}')
            if i % 50 == 0:
                save_cache(cache)
                log(f'  progress: {i}/{len(targets)} new={new_count} fail={fail_count}')
            time.sleep(SLEEP)
        save_cache(cache)
        log(f'  fetch done: new={new_count} fail={fail_count}')

    # apply
    updated = apply_to_db(db_path, cache)
    log(f'  UPDATE {db_path.name}: {updated} rows with blood applied')

    # verify
    conn = sqlite3.connect(db_path)
    still_null = conn.execute(
        "SELECT COUNT(*) FROM results WHERE date IN ('2026-04-11','2026-04-12') AND sire IS NULL"
    ).fetchone()[0]
    total = conn.execute(
        "SELECT COUNT(*) FROM results WHERE date IN ('2026-04-11','2026-04-12')"
    ).fetchone()[0]
    conn.close()
    log(f'  verify 4/11-12: {total-still_null}/{total} filled ({still_null} still NULL)')
    log(f'[blood] done {datetime.datetime.now().isoformat()}')

if __name__ == '__main__':
    main()
