# 💻 PC 入れ替え時の移行手順ガイド

作成: 2026-04-14
対象: のりおさん
目的: 今のPCから新しいPCに **norishiko_ai プロジェクトと Claude Code 環境をまるごと移す**

---

## 📋 最初にやること：**全体像の理解**

引越しで持っていくものを先に整理します。

### 🎒 新しいPCに持っていく3つの荷物

```
1. プロジェクトのコード（プログラム本体）
   → GitHub にすでに保存済み。新PCで git clone するだけ 🟢

2. 馬のデータベース（keiba.db 等）
   → 約800MB、GitHubには無い。USBメモリ等で直接コピー 🟡

3. Claude Code の設定と記憶
   → ログインと memory フォルダのコピー 🟡
```

---

## 🚨 移行前に**絶対やること**（今のPCで）

### ① 最新状態を GitHub に push しておく

今のPCのターミナルで:

```bash
cd /c/Users/westr/norishiko_ai
git status
```

**「nothing to commit, working tree clean」** と出ればOK。
もし何か変更が残ってたら:

```bash
git add -A
git commit -m "PC移行前の最終保存"
git push origin main
```

> ⚠️ これをやらないと、新PCに移した後に「あれ？最新の変更が消えてる！」となります

### ② keiba.db をバックアップ

最も大事なファイル。これが消えるとバックテストも予想も動きません。

```
C:\Users\westr\norishiko_ai\keiba.db  (約800MB)
```

**バックアップ方法**:
- USBメモリに直接コピー（推奨：32GB以上のUSB）
- 外付けHDD
- クラウドストレージ (Google Drive, OneDrive等)

> 💡 **推奨はUSBメモリ**。クラウドだと800MBアップロードに時間かかります。

### ③ 付属データファイルもコピー

以下のファイルも一緒にUSBに入れておくと便利:

```
C:\Users\westr\norishiko_ai\
  ├─ keiba.db                          ← 超重要 (約800MB)
  ├─ monthly_results_2026_04.json      ← 今月の結果
  ├─ weekend_predictions.json          ← 最新の予想
  ├─ this_week_races.json              ← 出馬表
  ├─ weekend_predictions_final.json    ← 最終予想スナップショット
  └─ docs/                             ← ドキュメント類 (一応)
```

**簡単な全体コピー方法**:
```
1. エクスプローラーで C:\Users\westr\norishiko_ai フォルダを開く
2. Ctrl+A で全選択
3. USBメモリの norishiko_ai_backup フォルダにコピー
```

容量目安: 全部で約1.5〜2GB

### ④ Claude Code の memory フォルダをバックアップ

```
C:\Users\westr\.claude\projects\C--Users-westr\memory\
```

このフォルダには Claude との会話履歴や記憶が入っています。全ファイルをUSBにコピー。

### ⑤ git のメールアドレスとユーザー名を確認

今のPCのターミナルで:

```bash
git config user.email
git config user.name
```

表示された内容をメモしておきます（例: `westr` と `kamonohashi0908@gmail.com`）。

---

## 🆕 新しいPCでやること

### STEP 1: 必要なソフトをインストール

#### ① Python をインストール

1. https://www.python.org/downloads/ にアクセス
2. 「Download Python 3.xx」ボタンをクリック
3. インストーラーを実行
4. **⚠️ 重要**: 「**Add Python to PATH**」のチェックボックスを**必ずON**にする
5. 「Install Now」をクリック

確認方法（ターミナルで）:
```bash
python --version
```
→ `Python 3.14.x` のように表示されればOK

#### ② Git をインストール

1. https://git-scm.com/download/win にアクセス
2. ダウンロードして実行
3. インストール中は基本的にデフォルトでOK
4. 「Git Bash Here」オプションは ON にしておく（便利）

確認:
```bash
git --version
```
→ `git version 2.xx.x` と表示されればOK

#### ③ Claude Code をインストール

1. https://claude.com/download にアクセス
2. 「Windows」版をダウンロード
3. インストール後、Anthropic アカウントでログイン

#### ④ Google Chrome をインストール（未インストールなら）

競馬データのスクレイピング（netkeiba からの取得）に Chrome が必要です。
https://www.google.com/chrome/ からダウンロード。

### STEP 2: プロジェクトを GitHub から取得

新PCでターミナル（Git Bash 推奨）を開き:

```bash
cd C:\Users\[新しいPCのユーザー名]
mkdir -p norishiko_ai
cd norishiko_ai
git clone https://github.com/norishico/norishico-ai.git .
```

> 💡 最後の `.` は「現在のフォルダにコピー」の意味。忘れずに。

### STEP 3: git のユーザー情報を設定

新PCのターミナルで（メモしておいたものを入力）:

```bash
git config --global user.name "westr"
git config --global user.email "kamonohashi0908@gmail.com"
```

### STEP 4: keiba.db とデータファイルを戻す

USBメモリからファイルを移動:

1. USBメモリを新PCに接続
2. `keiba.db` を `C:\Users\[新ユーザー名]\norishiko_ai\` にコピー
3. 他のデータファイルも同じ場所にコピー:
   - `monthly_results_2026_04.json`
   - `weekend_predictions.json`
   - `this_week_races.json`
   - `weekend_predictions_final.json`

### STEP 5: Python の必要なライブラリをインストール

ターミナルで:

```bash
cd C:\Users\[新ユーザー名]\norishiko_ai
pip install selenium numpy
```

> 💡 他のライブラリが必要になったら、エラーメッセージを見て追加インストール

### STEP 6: Chrome の自動操作ドライバー確認

Selenium は自動でChromeドライバーをダウンロードしますが、バージョン不一致が出た場合:

1. https://googlechromelabs.github.io/chrome-for-testing/ にアクセス
2. インストール済みChromeと同じバージョンの ChromeDriver をダウンロード
3. `chromedriver.exe` を Python のあるフォルダ、または PATH の通った場所に配置

### STEP 7: Claude Code のメモリ復元

1. USBメモリから `memory/` フォルダの中身をコピー
2. 新PCの以下の場所に貼り付け:
   ```
   C:\Users\[新ユーザー名]\.claude\projects\C--Users-westr\memory\
   ```
   > 注: `C--Users-westr` の部分は旧PCのパスなので、そのままにしておくのが無難

3. Claude Code を開いて、以前の記憶が引き継がれているか確認

### STEP 8: 動作確認テスト

ターミナルで以下を実行してエラーが出ないか確認:

```bash
cd C:\Users\[新ユーザー名]\norishiko_ai
python -c "import sqlite3; c=sqlite3.connect('keiba.db'); print('DB接続OK:', c.execute('SELECT COUNT(*) FROM results').fetchone())"
```

**成功パターン**:
```
DB接続OK: (335700,)
```
→ DBが正しくコピーされた証拠

### STEP 9: HTML 生成テスト

```bash
python generate_weekend_prediction.py
```

エラーが出なければ OK。`this_week_prediction.html` が生成されます。

---

## 🔧 トラブル時のチェックリスト

### ❌ Python が動かない

- [ ] Python インストール時に「Add to PATH」チェックしたか
- [ ] ターミナルを一度閉じて開き直したか
- [ ] `python` ではなく `py` で試してみる

### ❌ git clone が失敗する

- [ ] インターネットに繋がっているか
- [ ] GitHub にログインしているか（プライベートリポジトリの場合）
- [ ] Personal Access Token が必要なら設定

### ❌ keiba.db が読めない

- [ ] ファイルサイズが約800MBあるか（途中で壊れていないか）
- [ ] ファイル名が正確に `keiba.db` か（拡張子が .db になっているか）
- [ ] norishiko_ai フォルダの直下にあるか

### ❌ Selenium が動かない (Chrome エラー)

- [ ] Chrome 本体が最新か
- [ ] ChromeDriver のバージョンが Chrome と同じか
- [ ] Windows Defender がブロックしていないか

### ❌ auto_refresh.py が止まる

- [ ] Python の PATH が設定されているか
- [ ] 新PC で `py.exe` のパスが変わっている可能性
  → `auto_refresh.py` 冒頭の `PYEXE` 変数を新PCの Python パスに合わせる

### ❌ Claude Code の記憶がない

- [ ] memory/ フォルダのパスが合っているか
- [ ] `MEMORY.md` ファイルがコピーされたか
- [ ] Claude Code を再起動したか

---

## ⏱ 移行にかかる時間の目安

| 作業 | 時間 |
|---|---|
| 旧PC: バックアップ（push + USB コピー） | 30分〜1時間 |
| 新PC: ソフトインストール（Python, Git, Claude Code, Chrome） | 30分〜1時間 |
| 新PC: プロジェクト clone + ファイル配置 | 15分 |
| 新PC: ライブラリインストール + 動作確認 | 30分 |
| **合計** | **約2〜3時間** |

---

## 📦 もっと簡単な方法: 「フォルダごと丸コピー」

もし新旧PCを同時に使える環境で、かつ **Windows → Windows** なら、以下の方法がさらにシンプル:

### 必要なフォルダを丸ごとコピー

```
旧PC:
  C:\Users\westr\norishiko_ai\        ← 全部コピー
  C:\Users\westr\.claude\             ← 全部コピー

新PC:
  C:\Users\[新ユーザー名]\norishiko_ai\   ← 貼り付け
  C:\Users\[新ユーザー名]\.claude\         ← 貼り付け
```

**メリット**:
- 設定やキャッシュが全部そのまま使える
- 動作確認しやすい

**デメリット**:
- データ量が大きい（約5GB）
- 新PCにPython等の基本ソフトは別途インストール必要
- パスが `westr` → 新ユーザー名に変わる箇所は手動修正が必要

---

## 🎯 最重要ポイント5選

どうしても覚えられない場合、これだけは守ってください:

1. **🚨 git push を必ず先にやる** (コードの最新化)
2. **🚨 keiba.db を USB にバックアップ** (800MB の宝物)
3. **🚨 memory/ フォルダもバックアップ** (Claude の記憶)
4. **🚨 新PCで git clone → ファイル戻す → ライブラリインストール** の順
5. **🚨 動作確認** (DB読み込み → HTML生成 の2テスト)

---

## 🆘 どうしても困ったら

Claude Code で以下のように聞いてください:

```
「PCを移行して、○○ができないんだけど、どうしたらいい?」
「keiba.db がうまく読めません。エラーメッセージは[コピペ]です」
```

Claude はこのガイドを memory に持っているので、的確にサポートできます。

---

## 📚 関連ドキュメント

- `docs/OPERATIONAL_MONITORING.md` - 実運用監視
- `docs/OPERATIONAL_RISK_CHECKLIST.md` - 障害チェックリスト
- `docs/FEATURE_STORE_DESIGN.md` - 将来の基盤設計
- `docs/SESSION_2026_04_13.md` - v6.6完成記録
- `CLAUDE.md` - プロジェクト全体ガイド

---

## ✅ 移行完了チェックリスト

以下を全てチェックできたら移行完了です:

### 旧PCで済ませること
- [ ] git push で全変更を GitHub に反映
- [ ] keiba.db を USBにコピー
- [ ] monthly_results_*.json をコピー
- [ ] weekend_predictions*.json をコピー
- [ ] this_week_races.json をコピー
- [ ] memory/ フォルダをコピー
- [ ] git config のユーザー名・メールをメモ

### 新PCで済ませること
- [ ] Python 3.x インストール (PATH追加)
- [ ] Git インストール
- [ ] Claude Code インストール・ログイン
- [ ] Chrome インストール
- [ ] git clone でプロジェクト取得
- [ ] git config でユーザー情報設定
- [ ] keiba.db を配置
- [ ] データファイル群を配置
- [ ] memory/ フォルダ配置
- [ ] pip install selenium numpy 実行
- [ ] DB接続テスト成功 (`python -c "import sqlite3..."`)
- [ ] HTML生成テスト成功 (`python generate_weekend_prediction.py`)
- [ ] Claude Code で記憶が引き継がれているか確認

---

がんばってください！🚀
