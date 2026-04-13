# 🏗 Feature Store 設計ドキュメント

作成: 2026-04-14
作者: りさ (データエンジニア) + 12人委員会レビュー
状態: **設計段階**（実装はv6.6実運用安定後）
目的: 特徴量の一元管理基盤を作り、Phase II の特徴量追加を劇的に高速化する

## なぜ Feature Store が必要か

### 現状の問題
```
backtest_v6.py ────┐
predict_weekend.py ─┼─ 各自で scoring.py を呼んで毎回計算
analyze_*.py ──────┘
    ↓
    各スクリプトで:
    - 特徴量を毎回 scoring 内で計算
    - キャッシュは process scoped (メモリ)
    - 同じ計算を複数回
    - バグや不整合が発生しやすい
```

**具体的な痛み**:
1. **backtest_2026.py の accel_lap/race_name バグ** — prefetch SELECTと scoring fallback SELECTの不整合
2. **予測と検証の乖離リスク** — predict_weekend.py と backtest_v6.py で微妙に違う特徴量
3. **Phase II で特徴量を追加するたびに** 同じパターンのバグが起きる可能性
4. **計算が遅い** — 同じ馬の過去走を複数スクリプトが独立計算

### 理想像
```
keiba.db (生データ)
    ↓
feature_store.db (事前計算済み特徴量)
  ├─ horse_features    (馬ごと)
  ├─ race_features     (レース毎)
  ├─ market_features   (オッズ・市場情報)
  └─ meta_features     (時系列安定性メタ情報)
    ↓
全スクリプトが単一ソースを読む
    ↓
backtest / predict / analyze 全てが同じ特徴量
```

## スキーマ設計

### feature_store.db (SQLite)

#### テーブル1: horse_features
馬ごとの特徴量スナップショット。(horse_name, snapshot_date) で一意。

```sql
CREATE TABLE horse_features (
    horse_name TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,  -- スナップショット日付 (YYYY-MM-DD)
    
    -- 過去走集計 (直近5走)
    past_runs_count INTEGER,       -- 出走数
    past_wins INTEGER,             -- 勝利数
    past_top3 INTEGER,             -- 複勝数
    past_avg_last3f REAL,          -- 平均上がり3F
    past_best_last3f REAL,         -- ベスト上がり3F
    past_avg_margin REAL,          -- 平均着差
    
    -- 調教 (過去14日)
    train_best_lap1 REAL,          -- ベスト最終1F
    train_lap2 REAL,               -- 対応する2F前半
    train_source TEXT,             -- 'sakuro' or 'woodc'
    train_score REAL,              -- 閾値スコア
    train_has_good TEXT,           -- 好調教フラグ
    train_accel_lap TEXT,          -- 加速ラップフラグ
    
    -- 血統・コース相性
    sire_course_n INTEGER,         -- 同コースでの父産駒サンプル数
    sire_course_top3_rate REAL,    -- 同コースでの父産駒複勝率
    dam_sire_course_n INTEGER,
    dam_sire_course_top3_rate REAL,
    
    -- ローテ・前走
    interval_weeks INTEGER,        -- 前走からの週数
    prev_finish INTEGER,           -- 前走着順
    prev_distance INTEGER,
    prev_surface TEXT,
    surface_switch TEXT,           -- 転向(初ダート/初芝)フラグ
    
    -- 脚質
    running_style TEXT,            -- '逃げ'|'先行'|'中団'|'差追'
    
    PRIMARY KEY (horse_name, snapshot_date)
);
CREATE INDEX idx_hf_date ON horse_features(snapshot_date);
```

#### テーブル2: race_features
レースごとの特徴量。(race_id) で一意。

```sql
CREATE TABLE race_features (
    race_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    venue TEXT,
    race_num INTEGER,
    distance INTEGER,
    surface TEXT,
    track_cond TEXT,
    num_horses INTEGER,
    grade TEXT,
    
    -- ペース予測
    nige_count INTEGER,            -- 逃げ馬候補数
    predicted_pace TEXT,           -- 'H'|'M'|'S' (Hi/Mid/Slow)
    predicted_front_3f REAL,       -- 予測前半3F
    
    -- トラックバイアス
    week_num INTEGER,              -- 開催何日目
    track_bias_hint TEXT,          -- '外差し'|'逃げ残り'|'中団均等'等
    
    -- スコアリング結果
    scored_at TEXT,                -- スコアリング実行時刻
    top1_horse TEXT,               -- 本命
    top1_score REAL,
    top2_horse TEXT,
    top2_score REAL,
    gap REAL,                      -- top1-top2 差
    
    -- 買い判定
    buy_zone TEXT,                 -- 'normal'|'challenge'|NULL
    ev_calculated REAL,
    
    -- メタ
    source_version TEXT            -- 'v6.6' 等
);
```

#### テーブル3: market_features
オッズ市場情報。(race_id, horse_name, snapshot_time) で時系列。

```sql
CREATE TABLE market_features (
    race_id TEXT NOT NULL,
    horse_name TEXT NOT NULL,
    snapshot_time TEXT NOT NULL,    -- 'morning'|'T-30'|'T-10'|'final'
    odds REAL,
    popularity INTEGER,
    PRIMARY KEY (race_id, horse_name, snapshot_time)
);
CREATE INDEX idx_mf_race ON market_features(race_id);
```

**用途**: あかりの「オッズ変動履歴」特徴量の基盤

#### テーブル4: meta_features
特徴量自体のメタ情報 (安定性、パフォーマンス等)。

```sql
CREATE TABLE meta_features (
    feature_name TEXT PRIMARY KEY,
    build_timestamp TEXT,
    train_period_start TEXT,
    train_period_end TEXT,
    row_count INTEGER,
    notes TEXT
);
```

**用途**: 「この特徴量はいつのデータで計算されたか」の追跡

## ビルドパイプライン

```
build_feature_store.py
  ├─ Step 1: keiba.db から horse_features 構築 (全期間)
  ├─ Step 2: race_features 構築
  ├─ Step 3: market_features (初回は空、予測時に追記)
  └─ Step 4: meta_features 記録
```

### インクリメンタル更新戦略

- **日次**: 前日のレース結果を horse_features に追加
- **週次**: race_features を次週分事前計算
- **月次**: 血統・調教統計の再計算 (閾値の見直し)

## 既存コードとの統合

### フェーズ別ロードマップ

#### Phase A: 並列稼働 (リスク最小)
```python
# scoring.py
def score_past_performance(horse_name, race_date, ...):
    # 従来ロジック (fallback)
    if USE_FEATURE_STORE:
        cached = feature_store.get_horse_features(horse_name, race_date)
        if cached:
            return {'score': cached['computed_score']}
    # 元のロジック続行
    rows = sc.execute(...)
```

- 既存テストが通ることを確認しながら段階移行
- Feature Store の結果と現行ロジックの結果が一致するか毎回検証

#### Phase B: 段階切替
1. score_past_performance から移行
2. score_course_fitness
3. score_training_actual (既にキャッシュ存在)
4. score_bloodline
5. score_jockey_trainer

#### Phase C: 最適化
- 関連するキャッシュを Feature Store 経由に統一
- backtest_2026.py の prefetch_month を feature_store.build_* に置き換え
- 同一性チェック: backtest_v6.py 結果が完全に変わらないこと

## リスクと対策

### リスク1: 既存v6.6のバグ混入
- **対策**: 並列稼働フェーズでは読むだけ、書き込みは別ロジック
- **対策**: コミット前に backtest_v6.py で回帰テスト必須

### リスク2: Feature Storeのビルド失敗
- **対策**: インクリメンタル更新で部分失敗時は旧データを維持
- **対策**: build_feature_store.py はトランザクション使用

### リスク3: データ整合性
- **対策**: meta_features で「いつ・どの期間・どのコードバージョンで」構築したか追跡
- **対策**: 一致性チェックスクリプト `verify_feature_store.py`

## 実装スケジュール

### 前提条件
- v6.6 実運用が 2週間以上安定稼働していること
- BT乖離が ±5pt 以内であること

### 実装順序 (実運用安定後に開始)

| Week | 内容 |
|---|---|
| 1 | スキーマ定義 + build_feature_store.py Step1 (horse_features) |
| 2 | Step2 (race_features) + 初回ビルド + 検証 |
| 3 | Phase A 並列稼働（scoring.pyの1関数のみ置換）|
| 4 | 結果一致確認 + 次の関数の置換 |
| 5 | Phase B 継続 |
| 6 | market_features の運用開始 (Phase II 準備) |

### Phase II への波及

Feature Store が完成すれば、以下の追加特徴量が容易に:
- **オッズ変動履歴**: market_features に追記するだけ
- **パドック評価**: horse_features に列追加
- **騎手コメント感情**: horse_features or race_features に列追加
- **ML 特徴量**: Feature Store 全体を pandas DataFrame 化して lightgbm へ

## 開発ツール候補

- **SQLite**: 現行 keiba.db と同じで統一
- **alembic / migrate**: スキーマ変更管理 (将来検討)
- **pytest**: 一致性テスト自動化
- **pandas**: ML モデル学習時のロード用

## 判断基準：実装着手のタイミング

✅ 着手可能になる条件:
1. v6.6 実運用 ROI が BT期待値の ±10pt 以内で **4週間以上**安定
2. のりおの実運用習慣が確立 (土日の運用フローが固まる)
3. Phase II の本格着手が決定済み

🚫 着手を見送る条件:
1. v6.6 実運用で重大な問題発生中
2. BT期待値から -10pt 以上乖離
3. のりおが別の優先タスクに集中したい

## 委員会コメント

- **りさ**: 「これがあると Phase II の特徴量追加が劇的に楽になる。でも今作るべきではない。v6.6を安定させてから」
- **まなつ**: 「賛成。ML モデル追加時に Feature Store あると楽」
- **ゆきこ**: 「実装着手基準が明確で良い。見切り発車を防げる」
- **みなみ**: 「meta_features テーブルが良い。データのtraceabilityが上がる」
- **かえで**: 「Feature Store 完成後、較正モデルも再挑戦できる」
- **あかり**: 「market_features が Phase II の本命。早めに基盤を」
- **ひなた**: 「race_features のペース予測欄が楽しみ」
- **あおい**: 「騎手・厩舎の過去統計も horse_features に入れよう」
- **さくら**: 「血統3元相互作用もここに入れたい (サンプル増えたら)」
- **ゆめ**: 「調教師コメント列も予め設計に入れてあるの嬉しい」
- **れいな**: 「実装前に v6.6 安定が必須。同意」
- **りこ**: 「UI にも feature の可視化追加したい (将来)」

## 関連ドキュメント

- `docs/ROADMAP_CALIBRATION.md` - 全体ロードマップ
- `docs/OPERATIONAL_MONITORING.md` - v6.6 実運用監視
- `docs/OPERATIONAL_RISK_CHECKLIST.md` - 運用障害リスト
- `docs/CALIBRATION_PHASE_I_RESULTS.md` - Phase I 結果
