"""
ノリシコ競馬AI / build_db.py
TARGET CSV → keiba.db SQLite構築・差分更新

対応フォーマット（自動判別）:
  - 開催成績_ﾌﾙｾｯﾄ単勝*.csv  : 成績CSV（52カラム・ヘッダーなし）
  - 開催成績_配当A*.csv       : 配当CSV（224カラム）→ dividendsテーブル
  - ウッドC調教*.csv          : 調教CSV（40カラム・ヘッダーあり）
  - DE*.CSV                  : 出走表CSV
  - JT*.CSV                  : 単勝オッズCSV

使い方:
    python build_db.py 開催成績_ﾌﾙｾｯﾄ単勝__2019_0328
    python build_db.py 開催成績_ﾌﾙｾｯﾄ単勝__*.csv ウッドC調教*.csv
    python build_db.py --inspect somefile.csv    # カラム構造確認
"""

import pandas as pd
import numpy as np
import sqlite3
import sys
import re
from pathlib import Path

DB_PATH = "keiba.db"

# ══════════════════════════════════════════════════════════════
# 成績CSV（52カラム・ヘッダーなし）カラムマッピング
# 実データから確定済み（2025-03-28 解析）
# ══════════════════════════════════════════════════════════════
RESULTS_COL = {
    0:  'year_2d',        # 年(下2桁) 例:19
    1:  'month',          # 月
    2:  'day',            # 日
    3:  'kai',            # 回（第N回）
    4:  'venue',          # 競馬場名 例:'札幌'
    5:  'week_num',       # 開催週
    6:  'race_num',       # レース番号 1-12
    7:  'race_name',      # レース名
    8:  'num_horses',     # 頭数
    9:  'surface_raw',    # 芝/ダ
    10: 'turf_type',      # 芝内外(0=内,1=外)
    11: 'distance',       # 距離(m)
    12: 'track_cond',     # 馬場状態 良/稍/重/不良
    13: 'horse_name',     # 馬名
    14: 'sex',            # 性別
    15: 'age',            # 年齢
    16: 'jockey',         # 騎手名
    17: 'weight_kg',      # 斤量
    18: 'popularity',     # 人気
    19: 'finish',         # 着順
    20: 'horse_num',      # 馬番 ★正式馬番（HANDOVERのcol3に相当）
    21: 'pos_col',        # 枠番（1-8）
    22: 'jockey_change',  # 騎手変更フラグ
    23: 'margin',         # 着差（秒）
    24: 'prev_popularity',# 前走人気
    25: 'time_sec_raw',   # 走破タイム（秒×10、例:108.3=1分48秒3）
    26: 'time_raw',       # 走破タイム（MSSTT形式、例:1483=1分48秒3）
    27: '_unknown27',     # 不明（常に0）
    28: 'pos1',           # 1角通過順
    29: 'pos2',           # 2角通過順
    30: 'pos3',           # 3角通過順
    31: 'pos4',           # 4角通過順
    32: 'last3f',         # 上がり3F（秒）
    33: 'horse_weight',   # 馬体重（kg）
    34: 'trainer',        # 調教師名
    35: 'stable_loc',     # 所属（美/栗）
    36: 'prize_won',      # 収得賞金
    37: 'horse_id',       # 血統登録番号
    38: 'sire_id',        # 種牡馬ID
    39: 'dam_id',         # 母馬ID
    40: 'race_id_raw',    # レースID（連番）
    41: 'owner',          # 馬主名
    42: 'breeder',        # 生産者名
    43: 'sire',           # 父（種牡馬）名
    44: 'dam',            # 母馬名
    45: 'dam_sire',       # 母父（母の父）名
    46: 'coat_color',     # 毛色
    47: 'birthday_raw',   # 生年月日（YYMMDD）
    48: 'odds',           # 単勝オッズ
    49: '_unknown49',     # 不明（空白）
    50: '_unknown50',     # 不明（空白）
    51: 'prev_last3f',    # 前走上がり3F
}

# ══════════════════════════════════════════════════════════════
# 配当CSV（224カラム・ヘッダーなし）カラムマッピング
# ══════════════════════════════════════════════════════════════
DIVIDENDS_COL = {
    0:  'year_2d',
    1:  'month',
    2:  'day',
    3:  'kai',
    4:  'venue',
    5:  'week_num',
    6:  'race_num',
    7:  'race_name',
    8:  'num_horses',
    9:  'surface_raw',
    10: 'turf_type',
    11: 'distance',
    12: 'track_cond',
    13: 'heads',
    14: 'race_id_raw',
    # 単勝（87-88）
    87: 'tansho_umaban',
    88: 'tansho_payout',
    # 複勝（93-98: 3頭分）
    93: 'fukusho1_umaban',
    94: 'fukusho1_payout',
    95: 'fukusho2_umaban',
    96: 'fukusho2_payout',
    97: 'fukusho3_umaban',
    98: 'fukusho3_payout',
    # 馬連（115-118）
    115: 'umaren_uma1',
    116: 'umaren_uma2',
    117: 'umaren_payout',
    118: 'umaren_ninkibet',
    # ワイド（127-138: 3組分）
    127: 'wide1_uma1',
    128: 'wide1_uma2',
    129: 'wide1_payout',
    130: 'wide1_ninkibet',
    131: 'wide2_uma1',
    132: 'wide2_uma2',
    133: 'wide2_payout',
    134: 'wide2_ninkibet',
    135: 'wide3_uma1',
    136: 'wide3_uma2',
    137: 'wide3_payout',
    138: 'wide3_ninkibet',
    # 馬単（155-158）
    155: 'umatan_uma1',
    156: 'umatan_uma2',
    157: 'umatan_payout',
    158: 'umatan_ninkibet',
    # 3連複（179-183）
    179: 'sanrenpuku_uma1',
    180: 'sanrenpuku_uma2',
    181: 'sanrenpuku_uma3',
    182: 'sanrenpuku_payout',
    183: 'sanrenpuku_ninkibet',
    # 3連単（194-198）
    194: 'sanrentan_uma1',
    195: 'sanrentan_uma2',
    196: 'sanrentan_uma3',
    197: 'sanrentan_payout',
    198: 'sanrentan_ninkibet',
}


# ══════════════════════════════════════════════════════════════
# ユーティリティ
# ══════════════════════════════════════════════════════════════

def parse_finish(val) -> float:
    if pd.isna(val):
        return np.nan
    s = str(val).strip()
    s = s.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    try:
        return float(s)
    except:
        return 99.0


def parse_time_to_sec(val) -> float:
    """走破タイム MSSTT → 秒 (例: 1483 → 108.3秒)"""
    if pd.isna(val):
        return np.nan
    s = str(val).strip().replace('.', '')
    try:
        n = int(s)
        if n <= 0:
            return np.nan
        tenths = n % 10
        secs   = (n // 10) % 100
        mins   = n // 1000
        result = mins * 60 + secs + tenths / 10
        return result if 30 < result < 600 else np.nan
    except:
        return np.nan


def parse_date_ymd(year_2d, month, day) -> str:
    """年(2桁)/月/日 → YYYY-MM-DD (例: 19,7,27 → 2019-07-27)"""
    try:
        y2 = int(year_2d)
        year = 2000 + y2 if y2 < 50 else 1900 + y2
        return f"{year}-{int(month):02d}-{int(day):02d}"
    except:
        return ''


def parse_training_date(val) -> str:
    """調教日 YYYYMMDD → YYYY-MM-DD"""
    if pd.isna(val):
        return ''
    s = str(int(val)).strip()
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return ''


def detect_csv_type(filepath: str, df_sample: pd.DataFrame = None) -> str:
    name = Path(filepath).name.upper()
    if name.startswith('DE'):
        return 'shutsuba'
    if name.startswith('JT'):
        return 'odds'

    # ファイル名キーワード
    fname = Path(filepath).name
    if '調教' in fname or 'TRAINING' in fname.upper():
        return 'training'
    if '配当' in fname:
        return 'dividends'
    if '成績' in fname or 'ﾌﾙｾｯﾄ' in fname:
        return 'results'

    # カラム数で判定
    if df_sample is not None:
        ncols = len(df_sample.columns)
        if ncols == 52:
            return 'results'
        if ncols == 224:
            return 'dividends'
        if ncols == 40:
            return 'training'

    return 'results'


# ══════════════════════════════════════════════════════════════
# 成績CSV → resultsテーブル
# ══════════════════════════════════════════════════════════════

def parse_results_csv(filepath: str) -> pd.DataFrame:
    """成績CSV（52カラム・ヘッダーなし）を読み込んでDataFrameに変換"""
    df = pd.read_csv(filepath, encoding='cp932', header=None, low_memory=False,
                     dtype={40: str})  # race_id_rawは文字列で

    # カラム名付与（定義済みのもののみ）
    rename = {k: v for k, v in RESULTS_COL.items() if k < len(df.columns)}
    df = df.rename(columns=rename)

    # 日付生成
    df['date'] = df.apply(
        lambda r: parse_date_ymd(r['year_2d'], r['month'], r['day']), axis=1
    )

    # 表面（芝/ダ）
    df['surface'] = df['surface_raw'].astype(str).str.strip()

    # 走破タイム → 秒（col26のMSST形式を使用）
    if 'time_raw' in df.columns:
        df['time_sec'] = df['time_raw'].apply(parse_time_to_sec)

    # 着順の数値変換
    df['finish'] = df['finish'].apply(parse_finish)

    # race_id: date_venue_racenum 形式
    df['race_id'] = (df['date'] + '_' + df['venue'].astype(str) + '_' +
                     df['race_num'].astype(str))

    # 馬体重の前走比計算（同レースで自動計算できないため後処理）
    # horse_weight はそのまま保持

    # 不要カラム削除
    drop_cols = [c for c in df.columns if c.startswith('_unknown') or
                 c in ['year_2d', 'month', 'day', 'surface_raw', 'race_id_raw']]
    df = df.drop(columns=[c for c in drop_cols if c in df.columns])

    # 数値型変換
    num_cols = ['num_horses', 'distance', 'horse_num', 'age', 'weight_kg',
                'popularity', 'pos1', 'pos2', 'pos3', 'pos4', 'last3f',
                'horse_weight', 'odds', 'prize_won', 'race_num',
                'pos_col', 'kai', 'week_num', 'prev_last3f', 'prev_popularity']
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')

    # 障害レース除外
    df = df[~df['race_name'].astype(str).str.contains('障害|障', na=False)]

    return df


# ══════════════════════════════════════════════════════════════
# 配当CSV → dividendsテーブル
# ══════════════════════════════════════════════════════════════

def parse_dividends_csv(filepath: str) -> pd.DataFrame:
    """配当CSV（224カラム）を読み込んで配当テーブル形式に変換"""
    df = pd.read_csv(filepath, encoding='cp932', header=None, low_memory=False)

    rename = {k: v for k, v in DIVIDENDS_COL.items() if k < len(df.columns)}
    df = df.rename(columns=rename)

    df['date'] = df.apply(
        lambda r: parse_date_ymd(r['year_2d'], r['month'], r['day']), axis=1
    )
    df['race_id'] = (df['date'] + '_' + df['venue'].astype(str) + '_' +
                     df['race_num'].astype(str))

    # 必要カラムのみ抽出
    keep = ['race_id', 'date', 'venue', 'race_num', 'race_name',
            'num_horses', 'surface_raw', 'distance', 'track_cond',
            'tansho_umaban', 'tansho_payout',
            'fukusho1_umaban', 'fukusho1_payout',
            'fukusho2_umaban', 'fukusho2_payout',
            'fukusho3_umaban', 'fukusho3_payout',
            'umaren_uma1', 'umaren_uma2', 'umaren_payout',
            'wide1_uma1', 'wide1_uma2', 'wide1_payout',
            'wide2_uma1', 'wide2_uma2', 'wide2_payout',
            'wide3_uma1', 'wide3_uma2', 'wide3_payout',
            'umatan_uma1', 'umatan_uma2', 'umatan_payout',
            'sanrenpuku_uma1', 'sanrenpuku_uma2', 'sanrenpuku_uma3',
            'sanrenpuku_payout',
            'sanrentan_uma1', 'sanrentan_uma2', 'sanrentan_uma3',
            'sanrentan_payout', 'sanrentan_ninkibet',
            ]
    df = df[[c for c in keep if c in df.columns]]

    drop_cols = ['year_2d', 'month', 'day']
    df = df.drop(columns=[c for c in drop_cols if c in df.columns])

    return df


# ══════════════════════════════════════════════════════════════
# 調教CSV（ウッドC・40カラム・ヘッダーあり）→ trainingテーブル
# ══════════════════════════════════════════════════════════════

def parse_training_csv(filepath: str) -> pd.DataFrame:
    """ウッドC調教CSV（40カラム・ヘッダーあり）を読み込む"""
    df = pd.read_csv(filepath, encoding='cp932', low_memory=False)

    # 日付
    if '年月日' in df.columns:
        df['date'] = df['年月日'].apply(parse_training_date)
    elif '日付S' in df.columns:
        df['date'] = pd.to_datetime(
            df['日付S'].astype(str).str.replace(r'\s+', '', regex=True),
            errors='coerce'
        ).dt.strftime('%Y-%m-%d')

    # カラムマッピング
    rename_map = {
        '馬名':    'horse_name',
        '調教師':  'trainer',
        '場所':    'venue',
        'コース':  'course_code',
        'Lap1':    'lap1',        # ラスト1F個別ラップ ★スコアリング最重要
        'Lap2':    'lap2',        # ラスト2F個別ラップ
        '3F':      'time3',       # 3F累積
        '4F':      'time4',       # 4F累積
        '種牡馬名': 'sire',
        '母名':    'dam',
        '性別':    'sex',
        '年齢':    'age',
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    # '1F'カラム（累積ではなく個別）が残っていてlap1未設定なら使う
    if 'lap1' not in df.columns and '1F' in df.columns:
        df['lap1'] = pd.to_numeric(df['1F'], errors='coerce')
    if 'lap2' not in df.columns and '2F' in df.columns:
        # 2Fは累積なので 2F - 1F = lap2
        t1 = pd.to_numeric(df.get('1F', df.get('lap1', pd.Series(dtype=float))), errors='coerce')
        t2 = pd.to_numeric(df['2F'], errors='coerce')
        df['lap2'] = (t2 - t1).round(1)

    # lap1 数値変換・異常値除去
    if 'lap1' in df.columns:
        df['lap1'] = pd.to_numeric(df['lap1'], errors='coerce')
        df.loc[(df['lap1'] < 9) | (df['lap1'] > 16), 'lap1'] = np.nan

    # lap2 数値変換・異常値除去
    if 'lap2' in df.columns:
        df['lap2'] = pd.to_numeric(df['lap2'], errors='coerce')
        df.loc[(df['lap2'] < 9) | (df['lap2'] > 20), 'lap2'] = np.nan

    # source: ウッドCかどうか
    if 'course_code' in df.columns:
        df['source'] = df['course_code'].apply(
            lambda x: 'woodc' if str(x).upper() in ('D', 'W', 'WOODC', 'ウッド') else 'sakuro'
        )
    else:
        df['source'] = 'woodc'  # このファイルはウッドC専用

    # time3, time4 数値変換
    for c in ['time3', 'time4']:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')

    # 必要カラムのみ
    keep = ['date', 'horse_name', 'trainer', 'venue', 'source',
            'lap1', 'lap2', 'time3', 'time4', 'sire', 'dam', 'sex', 'age']
    df = df[[c for c in keep if c in df.columns]]

    # horse_name, date がないレコードは除外
    df = df.dropna(subset=['horse_name', 'date'])
    df = df[df['horse_name'].astype(str).str.strip() != '']

    return df


# ══════════════════════════════════════════════════════════════
# DB操作
# ══════════════════════════════════════════════════════════════

def init_db(conn: sqlite3.Connection):
    """テーブル・インデックスの初期化"""
    # resultsテーブル
    conn.execute("""
        CREATE TABLE IF NOT EXISTS results (
            race_id      TEXT,
            horse_name   TEXT,
            date         TEXT,
            venue        TEXT,
            race_num     INTEGER,
            race_name    TEXT,
            surface      TEXT,
            distance     INTEGER,
            track_cond   TEXT,
            finish       REAL,
            horse_num    INTEGER,
            pos_col      INTEGER,
            num_horses   INTEGER,
            jockey       TEXT,
            trainer      TEXT,
            weight_kg    REAL,
            horse_weight REAL,
            popularity   INTEGER,
            odds         REAL,
            time_sec     REAL,
            last3f       REAL,
            pos1         INTEGER,
            pos2         INTEGER,
            pos3         INTEGER,
            pos4         INTEGER,
            margin       REAL,
            sire         TEXT,
            dam          TEXT,
            dam_sire     TEXT,
            age          INTEGER,
            sex          TEXT,
            prize_won    REAL,
            kai          INTEGER,
            week_num     INTEGER,
            prev_last3f  REAL,
            prev_popularity INTEGER,
            stable_loc   TEXT,
            owner        TEXT,
            breeder      TEXT,
            coat_color   TEXT,
            jockey_change INTEGER,
            PRIMARY KEY (race_id, horse_name)
        )
    """)

    # dividendsテーブル
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dividends (
            race_id           TEXT PRIMARY KEY,
            date              TEXT,
            venue             TEXT,
            race_num          INTEGER,
            race_name         TEXT,
            tansho_umaban     INTEGER,
            tansho_payout     INTEGER,
            fukusho1_umaban   INTEGER,
            fukusho1_payout   INTEGER,
            fukusho2_umaban   INTEGER,
            fukusho2_payout   INTEGER,
            fukusho3_umaban   INTEGER,
            fukusho3_payout   INTEGER,
            umaren_uma1       INTEGER,
            umaren_uma2       INTEGER,
            umaren_payout     INTEGER,
            wide1_uma1        INTEGER,
            wide1_uma2        INTEGER,
            wide1_payout      INTEGER,
            wide2_uma1        INTEGER,
            wide2_uma2        INTEGER,
            wide2_payout      INTEGER,
            wide3_uma1        INTEGER,
            wide3_uma2        INTEGER,
            wide3_payout      INTEGER,
            umatan_uma1       INTEGER,
            umatan_uma2       INTEGER,
            umatan_payout     INTEGER,
            sanrenpuku_uma1   INTEGER,
            sanrenpuku_uma2   INTEGER,
            sanrenpuku_uma3   INTEGER,
            sanrenpuku_payout INTEGER,
            sanrentan_uma1    INTEGER,
            sanrentan_uma2    INTEGER,
            sanrentan_uma3    INTEGER,
            sanrentan_payout  INTEGER,
            sanrentan_ninkibet INTEGER
        )
    """)

    # trainingテーブル
    conn.execute("""
        CREATE TABLE IF NOT EXISTS training (
            horse_name TEXT,
            date       TEXT,
            trainer    TEXT,
            venue      TEXT,
            source     TEXT,
            lap1       REAL,
            lap2       REAL,
            time3      REAL,
            time4      REAL,
            sire       TEXT,
            dam        TEXT,
            sex        TEXT,
            age        INTEGER,
            PRIMARY KEY (horse_name, date, source)
        )
    """)

    # インデックス
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_res_race_id    ON results(race_id)",
        "CREATE INDEX IF NOT EXISTS idx_res_horse      ON results(horse_name)",
        "CREATE INDEX IF NOT EXISTS idx_res_date       ON results(date)",
        "CREATE INDEX IF NOT EXISTS idx_res_venue_dist ON results(venue, distance)",
        "CREATE INDEX IF NOT EXISTS idx_res_jockey     ON results(jockey)",
        "CREATE INDEX IF NOT EXISTS idx_res_trainer    ON results(trainer)",
        "CREATE INDEX IF NOT EXISTS idx_res_sire       ON results(sire)",
        "CREATE INDEX IF NOT EXISTS idx_res_dam_sire   ON results(dam_sire)",
        "CREATE INDEX IF NOT EXISTS idx_res_date_venue ON results(date, venue, race_num)",
        "CREATE INDEX IF NOT EXISTS idx_train_horse    ON training(horse_name)",
        "CREATE INDEX IF NOT EXISTS idx_train_date     ON training(date)",
        "CREATE INDEX IF NOT EXISTS idx_div_date       ON dividends(date, venue, race_num)",
    ]
    for sql in indexes:
        conn.execute(sql)
    conn.commit()


def upsert_df(conn: sqlite3.Connection, df: pd.DataFrame, table: str) -> int:
    """DataFrameをINSERT OR REPLACE でUPSERT"""
    if df.empty:
        return 0

    # テーブルの既存カラムを取得
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    # テーブルにないカラムはALTER TABLEで追加
    for col in df.columns:
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                print(f"    ➕ {table}.{col} カラム追加")
            except:
                pass

    # テーブルに存在するカラムのみ使用
    valid_cols = [c for c in df.columns if c in existing or c in df.columns]
    # 再確認
    existing2 = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    valid_cols = [c for c in df.columns if c in existing2]

    df_insert = df[valid_cols].copy()

    # NaN → None に変換（SQLite互換）
    df_insert = df_insert.where(pd.notnull(df_insert), None)

    cols_str      = ', '.join(valid_cols)
    placeholders  = ', '.join(['?'] * len(valid_cols))
    sql = f"INSERT OR REPLACE INTO {table} ({cols_str}) VALUES ({placeholders})"

    data = [list(row) for row in df_insert.itertuples(index=False, name=None)]
    conn.executemany(sql, data)
    conn.commit()
    return len(data)


# ══════════════════════════════════════════════════════════════
# メイン
# ══════════════════════════════════════════════════════════════

def build_db(csv_files: list, db_path: str = DB_PATH):
    print(f"\n{'═'*60}")
    print(f"  ノリシコ競馬AI — DB構築・更新")
    print(f"{'═'*60}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-65536")  # 64MB キャッシュ

    init_db(conn)

    totals = {'results': 0, 'dividends': 0, 'training': 0}

    for filepath in csv_files:
        print(f"\n  📂 {Path(filepath).name}")

        if not Path(filepath).exists():
            print(f"     ❌ ファイルが見つかりません")
            continue

        try:
            # ファイル種別判定（サンプル読み込み）
            try:
                sample = pd.read_csv(filepath, encoding='cp932', header=None, nrows=2)
                csv_type = detect_csv_type(filepath, sample)
            except:
                sample = None
                csv_type = detect_csv_type(filepath)

            print(f"     種別: {csv_type}")

            if csv_type == 'results':
                df = parse_results_csv(filepath)
                print(f"     {len(df):,}件読み込み")
                n = upsert_df(conn, df, 'results')
                print(f"     ✅ results +{n:,}件")
                totals['results'] += n

            elif csv_type == 'dividends':
                df = parse_dividends_csv(filepath)
                print(f"     {len(df):,}件読み込み")
                n = upsert_df(conn, df, 'dividends')
                print(f"     ✅ dividends +{n:,}件")
                totals['dividends'] += n

            elif csv_type == 'training':
                df = parse_training_csv(filepath)
                print(f"     {len(df):,}件読み込み (lap1有効: {df['lap1'].notna().sum()}件)")
                n = upsert_df(conn, df, 'training')
                print(f"     ✅ training +{n:,}件")
                totals['training'] += n

            elif csv_type == 'shutsuba':
                print(f"     ⚠️  出走表CSV: --inspect オプションで構造確認後に対応")

            elif csv_type == 'odds':
                print(f"     ⚠️  オッズCSV: JT形式は別途対応予定")

            else:
                print(f"     ❓ 不明な形式")

        except Exception as e:
            import traceback
            print(f"     ❌ エラー: {e}")
            traceback.print_exc()

    # ── 集計 ──────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    try:
        r = conn.execute("SELECT COUNT(*), COUNT(DISTINCT race_id), COUNT(DISTINCT horse_name), MIN(date), MAX(date) FROM results").fetchone()
        print(f"  📊 results:   {r[0]:>8,}件  {r[1]:,}R  {r[2]:,}頭  {r[3]}〜{r[4]}")
    except: pass
    try:
        d = conn.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM dividends").fetchone()
        print(f"  📊 dividends: {d[0]:>8,}件  {d[1]}〜{d[2]}")
    except: pass
    try:
        t = conn.execute("SELECT COUNT(*), COUNT(DISTINCT horse_name), MIN(date), MAX(date) FROM training").fetchone()
        print(f"  📊 training:  {t[0]:>8,}件  {t[1]:,}頭  {t[2]}〜{t[3]}")
    except: pass

    print(f"\n  今回の取込み: results+{totals['results']:,} / dividends+{totals['dividends']:,} / training+{totals['training']:,}")
    print(f"  保存先: {Path(db_path).absolute()}")
    print(f"{'═'*60}\n")

    conn.close()


def inspect_csv(filepath: str):
    """CSVのカラム構造を表示する診断ツール"""
    print(f"\n{'═'*60}")
    print(f"  CSV構造確認: {Path(filepath).name}")
    print(f"{'═'*60}")
    for enc in ['cp932', 'utf-8', 'utf-8-sig']:
        try:
            df = pd.read_csv(filepath, encoding=enc, header=None, nrows=5)
            print(f"  encoding={enc}, カラム数={len(df.columns)}, 行数(sample)={len(df)}")
            for i in range(len(df.columns)):
                vals = list(df.iloc[:3, i])
                print(f"  col{i:3d}: {vals}")
            break
        except Exception as e:
            print(f"  {enc}: NG ({e})")


if __name__ == '__main__':
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    if args[0] == '--inspect':
        for f in args[1:]:
            inspect_csv(f)
    else:
        build_db(args)
