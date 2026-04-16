# 実運用モニタリング パイプライン設計 (2026-04-15)

## 目的
v6.6 の実運用ROIがBT(全年125%)から乖離した際の早期検知。
単年ノイズで騒がず、構造変化を確実に捕捉する二段構えの検知器。

## 既存資産
- `monitor_actual.py` - monthly_results_*.json → BT基準比較 → Markdownレポート
- `docs/OPERATIONAL_MONITORING.md` - BT基準値 (7yr/直近2yr)
- `docs/monitoring_report.md` - 出力先
- `alerts_log.py` - アラート記録機構

## 不足していたもの (今回補填)

1. **自動実行**: Task Scheduler で毎週月曜朝に実行
2. **アラート連動**: RED 判定時に alerts_log に自動記録
3. **ダッシュボード統合**: `datasource_status.html` からリンク
4. **サンプル数不足の自動判定**: N<30 では warning を抑制 (単年ノイズ誤報防止)

## パイプライン構成

```
  monthly_results_*.json (日次追記)
         │
         ▼
  [scripts/run_monitoring.py]  ←── 週次 Task Scheduler
         │
         ├── monitor_actual.py 実行
         │     → docs/monitoring_report.md 更新
         │
         ├── 判定パース
         │     → GREEN/YELLOW/RED/INSUFFICIENT_SAMPLE
         │
         ├── RED のとき
         │     → alerts_log.write_alert('monitoring RED: ...')
         │     → logs/monitoring_alert_YYYYMMDD.json
         │
         └── ダッシュボード更新
               → scripts/generate_datasource_dashboard.py
               (monitoring_report のサマリを埋め込み)
```

## 判定閾値 (OPERATIONAL_MONITORING.md 準拠)

| 状態 | 条件 | アクション |
|---|---|---|
| 🟢+ GREEN+ | ROI ≥ 110% | 継続 |
| 🟢 GREEN | ROI ≥ allow基準 | 継続 |
| 🟡 YELLOW | warn ≤ ROI < allow | 次週再判定 |
| 🔴 RED | ROI < warn基準 | 即時アラート・委員会招集検討 |
| ⚪ INSUFFICIENT | N < 30 | 判定保留・継続観察 |

## 単年ノイズ誤報防止ルール (F1 2024 の教訓)

- N < 30 では verdict を出さず INSUFFICIENT 扱い
- 連続2週RED で初めて委員会招集
- 単月の赤字のみでは警告しない (累積判定のみ RED 扱い)
- クラス別RED でも総合GREENなら "局所警告" 止まり

## 実運用開始のチェックポイント

- [ ] `monthly_results_*.json` への bet_result 追記フローが機能している
  - 現状: 手動記入 or 既存スクリプト?
  - 自動化は Phase 2B (次課題)
- [ ] OPERATIONAL_MONITORING.md の BT基準値が v6.6 最新と一致
- [ ] scripts/run_monitoring.py が Task Scheduler に登録済み
- [ ] datasource_status.html に monitoring 行が追加されている

## 次期拡張 (スコープ外)
- bet_result の自動収集 (predict_weekend の買い目 + 実払戻で自動生成)
- 7日・30日ローリングウィンドウ ROI
- クラス×会場×オッズ帯 ピボット検知
- Slack/LINE 通知連携
