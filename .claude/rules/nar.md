---
paths:
  - "nar/**"
  - "nar_keiba.db"
---

# NAR (南関東地方競馬) 作業ルール

## ⚠️現在2系統の予想ロジックが存在する(混同注意)
1. **v3.3 (pick_honmei)** — 本番稼働中・実弾投入あり。`nar/scoring_nar.py`/`nar/predict_nar.py`/`nar/nar_daily.py`。下記「買い条件」参照
2. **H-NAR-1 (ペース免罪仮説)** — 2026-08-19設計、**まだ実弾投入していない**(2026-09からプロスペクティブ観測中、紙上検証のみ)。v3.3とは完全に独立したストリームで、v3.3のスコアリング・買い条件には一切影響しない。`nar/hnar1_pace_excuse.py`/`nar/backtest_hnar1.py`。採否は/committee最終審議待ち。詳細はメモリ`project_nar.md`・kanban「検証中」参照

## 会場コード
浦和=42 / 船橋=43 / 大井=44 / 川崎=45

## 買い条件 (v3.3 2026-04-28 確定、2026-08-20一部更新)
- score≥5.5 / odds 5.0-25.0 / 2位差≥2.0pt / 上がり3Fスコア追加(0.8pt上限)
- C3クラス除外(全会場) / 大井A系・B系クラス除外(大井C1/C2中心)
- **血統ボーナスは2026-08-20廃止済み**(リーク発見、修正版が無ボーナスより悪化のため。scoring_nar.pyから要素8削除、現在8要素)
- 5年WF CV: 185R ROI **122.1%**(旧127.6%はリーク込みの陳腐化した数値、2026年のみworst=23.6%だがn=22で統計的ブレの範囲、断定は9月以降のデータ待ち) (Winsorized 5万円キャップ)
- 年平均38R(週1ペース)が自然な限界。ピック数増加3路線は全不採用(2026-04-29)
- ⚠️ 浦和は直近3年(2024-2026)枯れ状態(0/9勝)。WF188%は2022年の偏り

## スクレイプ注意（IPブロック実績あり）
- ⚠️ nar.netkeiba.com のエンコーディングは固定ではない(2026年6月頃にEUC-JP→UTF-8へサイト側が切替した実績あり)。**Content-Typeヘッダのcharsetを優先し、euc-jpは未指定時のフォールバックに格下げする方式**で読むこと(`nar/build_nar_db.py`/`build_nar_db_fast.py`/`fetch_nar_shutsuba.py`参照)。決め打ちしない
- race_list_sub.html?kaisai_date=YYYYMMDD で race_id 一覧取得、結果は race/result.html?race_id= (requests可)
- **並列5以上+0.2s間隔でIPブロック**。ブロック中に再テストするとタイマーリセットで5時間以上長期化。2026-08-19/20には約4,000〜5,000件処理する毎にブロックが再発するパターンを2回連続で観測(時間経過で解消、再開前に単発疎通確認を)
- 安全設定: workers=2, sleep=0.5s

## DB操作の禁止事項
- **WALモードDBは shutil.copy2 でコピー禁止 → conn.backup(dest)** (PreToolUseフックでもブロックされる)
- `fix_nar_class_codes.py` は grid search をやり直してからでないと実行禁止
  (class_code変更→scoring_nar.pyのclass_drop計算が変わりBT ROI 103%→93%に悪化した実績)

## 主要ファイル
| ファイル | 役割 |
|---|---|
| nar_keiba.db | NAR専用DB (nar_races/nar_results/nar_dividends/nar_laps/nar_corner_pos/nar_pedigree_official) |
| nar/build_nar_db_fast.py | 過去成績取得 (並列, resume対応) |
| nar/scoring_nar.py | NAR専用スコアリング v3.3 (8要素、training dataなし) |
| nar/backtest_nar.py | NAR BT (--breakdown/--walkforward/--venue/--max-gap-req) |
| nar/nar_daily.py | 毎日自動パイプライン (予想JSON保存+結果照合) |
| nar/nar_pnl.py | 実運用P&L集計 |
| nar/hnar1_pace_excuse.py | H-NAR-1特徴量計算(trailing方式) ※実弾未投入 |
| nar/backtest_hnar1.py | H-NAR-1独立BT(v3.3非依存) ※実弾未投入 |
