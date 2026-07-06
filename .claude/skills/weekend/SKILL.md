---
name: weekend
description: 週末予想の生成・公開パイプラインの手動運用手順。「週末予想」「プレビュー生成」「予想HTML」「publish」「デプロイ」と言われた時に使う。自動化はタスクスケジューラ NorishikoAI_* 15本が主経路で、このスキルは手動介入・リカバリ用。
---

# 週末予想 生成・公開手順

## パイプライン全体
`publish_weekend.py` = fetch → predict → HTML生成 → deploy の一括実行。
```bash
py -X utf8 publish_weekend.py --saturday            # 土曜分
py -X utf8 publish_weekend.py --saturday --no-deploy --skip-fetch  # 部分実行
```
個別: `fetch_shutsuba.py` → `predict_weekend.py` → `generate_weekend_prediction.py`

## 自動実行の主経路（手動前に必ず確認 — 二重実行を避ける）
`schtasks /query | findstr NorishikoAI` で当日の予定を確認。
主要: SaturdayPreview(木21:00) / SundayPreview(土20:00) / MondayPromote(月6:30) / RaceDayAutoRefresh(土日9:00) / SundayDividends(日17:30)

## デプロイの注意（既知の落とし穴）
- **git push だけでは公開に反映されない**。`mc_keiba_public/` から `npx vercel deploy --prod` を自分で実行する
- レース日は auto_refresh.py の±20%オッズロックが必須機構（発走済みレースはスキップされる）

## 公開前チェック
1. 買い目の信頼度表示（★★★/★★/★）が条件どおりか
2. オッズがロック済み値か（発走直前の生オッズを出さない）
3. 馬名・着順・払戻はDB SELECTで照合済みか（推測禁止）
4. 払戻データ経路: HR配信は月曜午後。土日17:30のWebスクレイプが主経路（詳細はメモリ feedback_dividends_pipeline.md）
