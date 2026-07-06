# NORISHICO KEIBA AI — Claude Code 運用ガイド

## プロジェクト概要
JRAの成績・払戻データから期待値スコアを計算し、買い条件を満たすレースの買い目を生成する競馬予想AI。
NAR(南関東)版は `nar/` 配下 — 詳細ルールは `.claude/rules/nar.md`（nar/** 作業時に自動ロード）。

## 主要ファイル
| ファイル | 役割 |
|---|---|
| `keiba.db` | メインDB (results/dividends/training) |
| `scoring.py` | スコアリングエンジン (8要素+ボーナス群) |
| `backtest_v6.py` | v6確定版BT。**買い条件の正は `is_buy_v6()` / `is_special_buy()`** |
| `backtest_2026.py` | score_one_race / prefetch 共通関数 |
| `predict_weekend.py` / `publish_weekend.py` | 週末予想生成・公開 → 手順は `/weekend` |
| `auto_refresh.py` | レース日オッズ自動更新+±20%ロック（必須機構） |
| `fetch_and_build.py` / `jvlink_fetch.py` | JV-Link取得 (32bit Python, RACE+SLOP+WOOD) |
| `nar_keiba.db` + `nar/*.py` | NAR一式 → rules/nar.md |

自動化: Windowsタスクスケジューラ `NorishikoAI_*` 15本が主経路（`schtasks /query | findstr NorishikoAI`）。

## DBスキーマの罠（最重要）
- `results.horse_num` = **スコアランク連番（実馬番ではない）**。着順判定は `finish`
- `finish` は dividends の `umatan_uma1/uma2` から逆引き。照合必須
- `results.umaban` = 実馬番（JV-Link経由データはNULLあり）
- `training.lap1` = 最終1F（好調教: 坂路<12.0 / WC<11.5）、`lap1 < lap2` = 加速ラップ。horse_name は TRIM照合で JOIN

## 買い条件（v6.6 サマリ）
正はコード（`backtest_v6.py`）。不良馬場は全見送り / 1勝・2勝・G3は廃止 / バイアスフィルタあり。
配分（あかり案A）: 通常 = 単勝+馬連 2,000円 / チャレンジ・C2・F1 = 単勝のみ 1,000円。
NAR v3.3 は rules/nar.md。BT最新値は `python backtest_v6.py --year YYYY` で取得。

## SQLite高速化（必須）
```python
conn.execute("PRAGMA cache_size=-65536")
conn.execute("PRAGMA temp_store=MEMORY")
conn.execute("PRAGMA mmap_size=268435456")
```

## 🚨 必須ルール
1. **馬名・着順・払戻は必ずDBのSELECTで照合してから提示**（推測で馬名を書かない。文字化けページから読まない）
2. `horse_num` を実馬番と混同しない。`finish` は umatan_uma1/uma2 と照合
3. **オッズ幅1倍未満の買い条件は不採用**（normal≥2倍幅 / チャレンジ≥3倍幅。実効ROI = BT × 捕捉率）
4. **WALモードDBは shutil.copy 禁止 → `conn.backup(dest)`**（PreToolUseフックで強制ブロック）
5. **スコアリング・買い条件・通知/UIの変更は `/committee` で12人委員会に諮ってから実装**
6. **変更の検証は `/bt-verify`**（4年WF CV + Winsorized 5万円キャップで採否判断）
7. **変更前バックアップは .bak ファイルではなく git commit で取る**（.bak系は .gitignore 済み）
8. 記録（メモリ・CLAUDE.md）更新後は grep/Read で保存を検証し、結果を報告する

## 作業スタイル
- 実行前に所要時間を見積もって提示。20%以上遅れたら即原因調査
- エラーは3回まで自己修正してから報告。結果は必ず数字で示す（「速くなった」ではなく「X倍速」）
- `keiba_tmp_*.db` はBT後に毎回削除（Stopフックでも自動掃除）
- プロジェクト現状・委員会メンバー詳細はメモリ `project_norishiko.md` 参照（このプロジェクトのメモリディレクトリに移住済み）
