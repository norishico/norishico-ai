# オッズ時系列ロギング設計案（A8）

2026-07-12 Fableブレインストーミング由来。**2026-07-18 /committee承認・実装済み**
（`auto_refresh.py: save_odds_snapshot()`、`quick_odds_refresh()`に統合済み）。
分析は8-12週間データが貯まってから着手する方針は据え置き。

**⚠️ 保存先はkeiba.dbではなく専用DB `odds_snapshots.db`**（2026-07-18、実装直後に発覚・修正）。
keiba.dbに置いた初版は、fetch_and_build.pyのatomic_swap（staging保護分岐でclone省略時）が
staging側に無いテーブルごとprodを丸ごと置換し、蓄積データが消滅する実害が即日発生したため。
odds_snapshotsはJV-Linkパイプラインと無関係な独立データなので、専用DBに分離するのが正しい設計。

## 目的

netkeiba/JV-Linkの前日〜直前オッズは現在保存しておらず、遡及取得が不可能。「オッズの変動
そのもの」（他の参加者の情報の集約過程）を将来分析するための土台として、まず収集だけを
開始する。分析は8-12週間分のデータが貯まってから着手する（先行投資型）。

## 新規テーブル案

```sql
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT NOT NULL,        -- date_venue_race_num 形式（既存race_id体系に合わせる）
    date TEXT NOT NULL,
    venue TEXT NOT NULL,
    race_num INTEGER NOT NULL,
    umaban INTEGER NOT NULL,
    horse_name TEXT,
    odds REAL,
    popularity INTEGER,
    snapshot_type TEXT NOT NULL,  -- 'morning' | 'pre_race_10min' | 'friday_night' 等
    fetched_at TEXT NOT NULL      -- ISO8601 timestamp
);
CREATE INDEX IF NOT EXISTS idx_odds_snap_race ON odds_snapshots(race_id, snapshot_type);
```

## 収集タイミング（既存の取得インフラに便乗、新規スクレイピング不要）

| snapshot_type | 既存の取得ポイント | 追加作業 |
|---|---|---|
| morning | `auto_refresh.py`の朝実行（morning_snapshot.json生成時） | 取得済みオッズをDBにも書き込むだけ |
| pre_race_10min | `auto_refresh.py`のレース前±20%ロックチェック時 | 同上 |
| （任意）friday_night | 現状取得ポイントなし、新規追加が必要なら別途検討 | 新規cron/schtasks要 |

## 実装イメージ（レビュー用、まだauto_refresh.pyには組み込んでいない）

```python
def save_odds_snapshot(conn, race_id, date, venue, race_num, horses, snapshot_type):
    """horses: [{'umaban':int,'horse_name':str,'odds':float,'popularity':int}, ...]"""
    now = datetime.now().isoformat()
    rows = [(race_id, date, venue, race_num, h['umaban'], h.get('horse_name'),
             h.get('odds'), h.get('popularity'), snapshot_type, now) for h in horses]
    conn.executemany("""
        INSERT INTO odds_snapshots
        (race_id,date,venue,race_num,umaban,horse_name,odds,popularity,snapshot_type,fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, rows)
    conn.commit()
```

`auto_refresh.py`内の既存オッズ取得箇所（morning_snapshot.json書き出し部分、±20%ロック
チェック部分）の直後にこの関数呼び出しを1行ずつ追加するだけで収集が始められる見込み。

## 将来の分析計画（8-12週間後）

1. 朝オッズ→直前オッズの変動率（drift）を算出
2. driftの大きい馬（人気急上昇/急降下）が、直前オッズだけを使った予測より実際の成績が
   良い/悪いかを検証（真の情報が乗っているか、それとも単なるノイズか）
3. 有意な信号があれば、scoring/MCへの追加特徴量として正式にWF検証

## 未決事項（実装前にのりお確認）

- 収集頻度を増やす場合（金曜夜等）の追加取得ポイント新設要否
- `odds_snapshots`テーブルの肥大化対策（1日あたり全国36場×16頭×2時点=概算1,000行程度/日、
  年間36万行程度の見込み。既存DBサイズへの影響は軽微と推測されるが要確認）
- 実装・DB変更は`/committee`承認後に着手
