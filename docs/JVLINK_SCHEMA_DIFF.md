# JV-Link ↔ netkeiba スキーマ差分表 (Phase 2 Step 1)

作成日: 2026-04-15
対象: `keiba.db` (results / dividends)
比較元: `jvlink_fetch.py` (`_se_row` 52列 / `_hr_row` 224列) vs `build_db.py` RESULTS_COL / DIVIDENDS_COL

## 結論サマリ

- **列構造は完全一致**: jvlink_fetch.py の出力CSVは build_db.py の列マッピング
  (RESULTS_COL 52列 / DIVIDENDS_COL 224列) にそのまま流し込める。
- **差は「列の欠損」ではなく「値の未取得」**: 一部列を JV-Link 側では空文字で出力する。
  これらは netkeiba 経由の従来取得で埋まっていた項目であり、並行運用中に
  「JV-Link 単独でモデルが動くか」を検証する要注意項目。
- **致命傷レベルの欠落はなし**: scoring で必須の
  `accel_lap` は training テーブル側で、
  `race_name / num_horses / finish / odds / horse_num / sire / dam_sire /
  horse_weight / pos1-4 / last3f` はすべて JV-Link から取得可能。

---

## results テーブル 値充足差分

| # | 列 | netkeiba | JV-Link | 影響度 | 備考 |
|---|---|---|---|---|---|
| 22 | jockey_change | ○ | **空** | 低 | 現行scoringで未使用 |
| 23 | margin | ○ | **空** | 低 | 現行scoringで未使用 |
| 24 | prev_popularity | ○ | **空** | 中 | 過去走スコア成分で使うが SE に直接はない |
| 25 | time_sec_raw | ○ | (空) | なし | 下流で time_raw から再計算される |
| 27 | _unknown27 | - | - | なし | 予約 |
| 36 | prize_won | ○ | △UM経由 | 中 | UMキャッシュ hit 時のみ(`prize_heichi`)。初回取得時は空 |
| 40 | race_id_raw | ○ | **空** | 低 | race_id は year+monthday+jyo+... から復元可 |
| 49/50 | _unknown49/50 | - | - | なし | 予約 |
| 51 | prev_last3f | ○ | **空** | 中 | 過去走参照で使う。内部結合で補完可能 |

### 致命傷レベル (scoring 必須) — JV-Linkで埋まる列
`horse_name / finish / horse_num / pos_col / num_horses / odds / popularity /
time_raw / last3f / pos1-4 / horse_weight / trainer / stable_loc /
sire / dam / dam_sire / horse_id / sire_id / dam_id / owner / breeder /
coat_color / birthday_raw / race_name / venue / distance / surface_raw /
turf_type / track_cond / jockey / weight_kg / sex / age` — すべて充足

---

## dividends テーブル 値充足差分

| # | 列 | netkeiba | JV-Link | 影響度 | 備考 |
|---|---|---|---|---|---|
| tansho_ninki | | ○ | **空** | 低 | build_db側で取得していないためどちらもnull |
| fukusho*_ninki | | ○ | **空** | 低 | 同上 |
| umaren/wide/umatan/sanrenpuku/sanrentan ninkibet | | ○ | **○** | - | JV-Linkから取得できる |
| 全払戻カラム | ○ | ○ | - | 完全一致 |

dividends 側は **実質差分なし**。払戻・組合せは JV-Link の HR レコードから完全に再現される。

---

## training テーブル (補助データ)

JV-Link の RACE dataspec には含まれない。現行 `training` テーブルは
netkeiba 経由で別建て取得しており、本タスクのスコープ外。
**Phase 2では training は netkeiba 継続** とし、results / dividends のみを並行検証対象とする。

---

## Phase 2 並行検証で監視するメトリクス

1. **レース粒度**: `race_id` の集合差 (JV-Link にあって netkeiba にない、逆も)
2. **出走馬粒度**: `(race_id, horse_name)` 単位で両DBに存在するか
3. **値ズレ**: `finish / odds / time_raw / last3f / pos1-4 / horse_weight` の一致率
4. **払戻ズレ**: `tansho_payout / umaren_payout` の一致率
5. **血統列ズレ**: `sire / dam / dam_sire` の一致率(表記ゆれ検知)
6. **欠落値率**: 上記の「△/空」列の NULL率 (実運用で scoring が動くか判定)

合意基準:
- 上記 1-2 の集合差: **0件**
- 上記 3-4 の一致率: **99% 以上**
- 上記 5 (血統表記): 一致率 **95% 以上** (表記ゆれ許容)
- 上記 6 の NULL 率: prize_won で 20% 未満、prev_last3f はモデル側で NULL耐性確認
