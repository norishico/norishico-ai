"""
staging→prod 差分マージ (2026-04-15 漏れ週末分)
keiba_staging.db の指定日付の results/dividends を keiba.db へコピー。
"""
import sqlite3, sys, datetime, pathlib

PROJ = pathlib.Path(__file__).resolve().parent.parent
PROD = PROJ / 'keiba.db'
STAGING = PROJ / 'keiba_staging.db'
DATES = ('2026-04-11', '2026-04-12')
LOG = PROJ / 'logs' / f'merge_{datetime.datetime.now():%Y%m%d_%H%M%S}.log'

def log(msg):
    print(msg)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(msg + '\n')

def main():
    LOG.parent.mkdir(exist_ok=True)
    log(f'[merge] start {datetime.datetime.now().isoformat()}')
    log(f'  prod={PROD} staging={STAGING} dates={DATES}')

    conn = sqlite3.connect(PROD)
    conn.execute(f"ATTACH DATABASE '{STAGING}' AS stg")

    for tbl in ('results', 'dividends'):
        before = conn.execute(f'SELECT COUNT(*) FROM {tbl}').fetchone()[0]
        placeholders = ','.join('?' * len(DATES))
        inserted = conn.execute(
            f'INSERT OR IGNORE INTO main.{tbl} SELECT * FROM stg.{tbl} WHERE date IN ({placeholders})',
            DATES,
        ).rowcount
        after = conn.execute(f'SELECT COUNT(*) FROM {tbl}').fetchone()[0]
        log(f'  {tbl}: before={before:,} inserted={inserted} after={after:,} (+{after-before})')

    conn.commit()

    for tbl in ('results', 'dividends'):
        for d in DATES:
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE date=?", (d,)).fetchone()[0]
            log(f'  verify {tbl} {d}: {n}')

    conn.close()
    log(f'[merge] done {datetime.datetime.now().isoformat()}')
    log(f'  log saved: {LOG}')

if __name__ == '__main__':
    main()
