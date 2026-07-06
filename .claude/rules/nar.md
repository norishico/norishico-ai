---
paths:
  - "nar/**"
  - "nar_keiba.db"
---

# NAR (南関東地方競馬) 作業ルール

## 会場コード
浦和=42 / 船橋=43 / 大井=44 / 川崎=45

## 買い条件 (v3.3 2026-04-28 確定・変更なし)
- score≥5.5 / odds 5.0-25.0 / 2位差≥2.0pt / 上がり3Fスコア追加(0.8pt上限)
- C3クラス除外(全会場) / 大井A系・B系クラス除外(大井C1/C2中心)
- 5年WF CV: 192R ROI 127.6%, worst=67.5%(2026) (Winsorized 5万円キャップ)
- 年平均38R(週1ペース)が自然な限界。ピック数増加3路線は全不採用(2026-04-29)
- ⚠️ 浦和は直近3年(2024-2026)枯れ状態(0/9勝)。WF188%は2022年の偏り

## スクレイプ注意（IPブロック実績あり）
- nar.netkeiba.com は **EUC-JP** エンコーディング
- race_list_sub.html?kaisai_date=YYYYMMDD で race_id 一覧取得 (requests可)
- **並列5以上+0.2s間隔でIPブロック**。ブロック中に再テストするとタイマーリセットで5時間以上長期化
- 安全設定: workers=2, sleep=0.5s

## DB操作の禁止事項
- **WALモードDBは shutil.copy2 でコピー禁止 → conn.backup(dest)** (PreToolUseフックでもブロックされる)
- `fix_nar_class_codes.py` は grid search をやり直してからでないと実行禁止
  (class_code変更→scoring_nar.pyのclass_drop計算が変わりBT ROI 103%→93%に悪化した実績)

## 主要ファイル
| ファイル | 役割 |
|---|---|
| nar_keiba.db | NAR専用DB (nar_races/nar_results/nar_dividends) |
| nar/build_nar_db_fast.py | 過去成績取得 (並列, resume対応) |
| nar/scoring_nar.py | NAR専用スコアリング (training dataなし) |
| nar/backtest_nar.py | NAR BT (--breakdown/--walkforward/--venue/--max-gap-req) |
| nar/nar_daily.py | 毎日自動パイプライン (予想JSON保存+結果照合) |
| nar/nar_pnl.py | 実運用P&L集計 |
