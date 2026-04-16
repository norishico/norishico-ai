# NORISHICO KEIBA AI — Claude Code 運用ガイド

## プロジェクト概要
JRAの成績・払戻データから期待値スコアを計算し、買い条件を満たすレースの買い目を生成する競馬予想AI。

---

## 主要ファイル
| ファイル | 役割 |
|---|---|
| `keiba.db` | メインDB (results/dividends/training) |
| `scoring.py` | スコアリングエンジン (8要素+ボーナス群) |
| `backtest_v6.py` | v6確定版バックテスト (クラス別フィルタ) |
| `backtest_2026.py` | score_one_race / prefetch 共通関数 |
| `predict_weekend.py` | 週末予想生成 |
| `auto_refresh.py` | レース日オッズ自動更新+±20%ロック |
| `publish_weekend.py` | fetch→predict→HTML→deploy パイプライン |
| `generate_weekend_prediction.py` | 予想HTML生成 |
| `fetch_and_build.py` | JV-Link取得→staging DB構築→血統+調教自動連鎖 |
| `jvlink_fetch.py` | JV-Link直接取得 (32bit Python, RACE+SLOP+WOOD) |

---

## DBスキーマ（重要）

### results テーブル
- `horse_num` = スコアランク連番（**実馬番ではない**）
- `finish` = dividendsのumatan_uma1/uma2から逆引き設定
- `umaban` = 実馬番（4/11-12のJV-Link経由データはNULL）

### training テーブル
| カラム | 内容 |
|---|---|
| `horse_name` | 馬名（TRIM照合でresultsとJOIN） |
| `date` | 調教日 |
| `venue` | 美浦/栗東 |
| `source` | `sakuro`(坂路) / `woodc`(ウッドチップ) |
| `lap1` | **最終1Fタイム**（好調教: 坂路<12.0 / WC<11.5） |
| `lap2` | 前1F（**加速ラップ判定: lap1 < lap2**） |

### dividends テーブル
- `tansho_payout`〜`sanrentan_payout`: 各式払戻
- `umatan_uma1/uma2`: 1着2着実馬番（信頼できる）

---

## 買い条件サマリ（v6.6）
※コード詳細は `backtest_v6.py` の `is_buy_v6()` / `is_special_buy()` 参照

| クラス | 通常ゾーン | チャレンジ | 備考 |
|---|---|---|---|
| 新馬 | - | C2別枠 (非主流血統×accel×15頭+×odds10-20) | 単勝1,000円 |
| 未勝利 | - | F1別枠 (主流血統×好調教×accel×odds15-33×1-8人気) | 単勝1,000円 |
| 1勝/2勝/G3 | **廃止** | **廃止** | |
| 3勝 | odds 8-11 (accel必須, heads12+/gap8+) | odds 20-25 | SS系非好調教除外 |
| G1/G2 | - | odds 7-10, 13-20 (内部赤字帯除外) | |
| 全クラス | **不良馬場は全見送り** | | ROI 26% |

**バイアスフィルタ**: 内枠(≤35%)で新潟芝後半/札幌芝後半/中山ダ前半 → 見送り

### 買い目配分（あかり案A）
| 区分 | 買い目 | 投資 |
|---|---|---|
| 通常(odds≥8) | 単勝◎500円 + 馬連◎○1,500円 | 2,000円 |
| 通常(odds<8) | 単勝◎1,000円 + 馬連◎○1,000円 | 2,000円 |
| チャレンジ/C2/F1 | 単勝◎1,000円のみ | 1,000円 |

### 信頼度表示
| 条件 | 表示 |
|---|---|
| v6通常 + gap≥10 + 好調教 + 血統ボーナス | ★★★ 自信の一戦 |
| v6通常（上記以外） | ★★ 注目レース |
| チャレンジ/C2/F1 | ★ チャレンジ枠 |

---

## ボーナステーブル群
全て `cutoff_date` 前のデータで構築（リーク防止）、`backtest_full.py` で年度別リビルド。

| テーブル | 関数 | 範囲 | 構築条件 |
|---|---|---|---|
| venue_sire_bonus | calc_venue_sire_bonus | 0〜5pt | n≥30, diff≥+12, stability≥50% |
| venue_damsire_bonus | calc_venue_damsire_bonus | 0〜3pt | 同上（係数控えめ） |
| cushion_sire_bonus | calc_cushion_sire_bonus | 0〜1.5pt | 芝のみ, n≥30, diff≥+8, 3ビン(soft/normal/firm) |
| nicks_bonus | calc_nicks_bonus | 0〜1.5pt | 芝のみ, n≥100, diff≥+10, **min_patterns<10でスキップ** |
| daily_track_bias | calc_daily_bias_bonus | (未使用) | 不採用(効果+800ノイズ) |

---

## SQLite高速化（必須）
```python
conn.execute("PRAGMA cache_size=-65536")
conn.execute("PRAGMA temp_store=MEMORY")
conn.execute("PRAGMA mmap_size=268435456")
```

---

## F1 2024不振 → 結論: 単年ノイズ確定（v6.6ルール現行維持）

---

## BT結果
最新値は `python backtest_v6.py --year YYYY` で取得。memory に Winsorized baseline 記載。

---

## 🚨 必須ルール（エラー防止）
1. **馬名・着順・払戻を提示する際は必ず外部データで照合してから回答する**
2. 推測で馬名を書かない。DBの `SELECT` で直接出力してから確認する
3. `horse_num` は実馬番ではなくスコアランク。着順判定には `finish` を使う
4. `finish` の確認には `umatan_uma1/uma2` と dividends を照合する
5. 文字化けした外部ページから馬名を読み取らない

## 🚨 実運用現実性ルール（バックテストの罠防止）
1. **オッズ幅1倍未満の買い条件は不採用**（発走時±0.3-0.5倍変動で圏外）
2. BT高ROIでも、バンド幅が狭ければ **実効ROI = BT × 捕捉率**
3. 1勝/2勝は構造的に実運用不可（完全廃止）
4. normal買い: **最低2倍幅以上**、チャレンジ: **最低3倍幅以上**
5. **auto_refresh ±20%ロック機構は必須**

---

## DB新規構築手順
1. JRA-VANから成績CSV・払戻CSVを取得
2. `build_db.py` で results/dividends テーブル構築
3. `build_supplementary_tables.py` で補助テーブル生成
4. dividends の umatan_uma1/uma2 から finish 逆引き更新

---

## スクリプト変更ルール
スクリプトを変更する前に必ずバックアップファイルを作成する

---

## 議論・意思決定のルール
ルール変更・条件調整・スコアリング変更は**12人委員会で議論してから実装**する。

### メンバー（12人）
- みなみ(26): データ分析・EV理論派。数字で判断
- れいな(29): 現場観察派。「実際に買えるか」の感覚
- ゆきこ(31): リスク管理・Kelly基準。設計の一貫性
- さくら(24): 血統・調教専門。穴馬発掘
- あかり(27): オッズ市場分析。過小評価馬を狙う
- ひなた(28): 展開・ペース予測。脚質相性重視
- あおい(30): 騎手・厩舎データ。ローテ読み
- まなつ(25): 統計ML。過学習に敏感、SHAP必須
- りこ(23): UI/UX。ユーザー目線で情報設計
- かえで(30): ベイズ推論・確率キャリブレーション
- りさ(26): データエンジニア・特徴量基盤
- ゆめ(24): 元JRA厩舎スタッフ・ドメイン暗黙知

### 進め方
1. 変更提案 → 12人で賛否・懸念点を議論
2. のりお（ユーザー）に確認
3. 承認後に実装 → **4年Walk-Forward CV（通常+Winsorized）で検証**
4. 結果を12人に評価させて最終判断

### 口調
みなみ:論理的クール / れいな:直感的ハッキリ / ゆきこ:冷静体系的 / さくら:熱血 / あかり:クール皮肉屋 / ひなた:おっとり鋭い / あおい:サバサバ / まなつ:早口「検証しよう」 / りこ:穏やか芯強い

---

## 処理パフォーマンスルール
1. 実行前に所要時間を見積もって提示する
2. 見積もりより20%以上遅れたら即座に原因調査
3. SQLite PRAGMA最適化・キャッシュ活用・バッチ処理を意識する

## 作業スタイル
- 変更前に必ずバックアップを取る
- エラーは自分で3回まで修正を試みてから報告する
- 結果は必ず数字で示す（「速くなりました」ではなく「X倍速になりました」）
- スコアリング・買い条件の変更は委員会に諮ってから実装する
- **記録した際には正しく記録できているか確認する**
  - メモリ・CLAUDE.md 更新後は grep/Read でキーワード保存を検証
  - 「書き込んだつもり」で終わらせず確認までワンセット
  - 確認結果もユーザに報告する
- **BT検証時は `keiba_tmp_*.db` を毎回削除**（NEW2: 汚染防止）
- **scoring変更はWinsorized ROI（50,000円キャップ）で判断**（NEW1）
