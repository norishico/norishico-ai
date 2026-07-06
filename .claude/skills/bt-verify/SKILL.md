---
name: bt-verify
description: バックテスト検証の標準手順。スコアリング・買い条件を変更した後、「BT」「バックテスト」「WF CV」「ROI検証」を求められた時に使う。Walk-Forward CV＋Winsorized判定＋tmp DB掃除まで一式。
---

# バックテスト検証手順

## 基本コマンド
```bash
python backtest_v6.py --year YYYY        # JRA 単年
python nar/backtest_nar.py --walkforward # NAR（--breakdown/--venue/--max-gap-req あり）
```

## 検証プロトコル（変更採否はこの順で）
1. **4年 Walk-Forward CV**（通常 + Winsorized の両方）
2. **採否判断は Winsorized ROI（払戻5万円キャップ）で行う** — 生ROIは大穴1本で歪む
3. **v6.6ベースラインと常時比較** — 改善幅と worst year を必ず併記
4. 2026年は holdout（チューニングに使わない）
5. 結果は数字で示す（N件 / ROI% / 収支円 / worst year）

## 実運用現実性チェック（BTの罠防止）
- オッズ幅1倍未満の買い条件は不採用（発走時±0.3-0.5倍変動で圏外になる）
- normal買いは最低2倍幅、チャレンジは最低3倍幅
- BT高ROIでも 実効ROI = BT × 捕捉率 で考える
- auto_refresh の±20%ロック機構が前提

## 後始末（必須）
- `keiba_tmp_*.db` を毎回削除（Stopフックでも自動掃除されるが、BT直後に自分でも確認）
- 採用時: ボーナステーブルは cutoff_date 前データで年度別リビルド（backtest_full.py、リーク防止）
- 結果をメモリ project_norishiko.md に追記し、grep で保存確認
