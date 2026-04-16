# ======================================================================
#  TFJV (TARGET frontier JV) 自動同期スクリプト
#  - TFJV.EXE を起動 → JV-Linkで自動同期 → 強制終了
#  - CK_DATA (調教データ) フォルダの更新を検知してログ出力
#
#  注意: TFJVはGUIアプリなのでヘッドレス実行不可。
#  起動時にフラッシュするのは仕様。
# ======================================================================
param(
  [int]$WaitSec = 90
)

$TFJVExe = "C:\TFJV\TFJV.EXE"
$CKDataRoot = "C:\TFJV\CK_DATA"
$LogDir = "C:\Users\westr\norishiko_ai\logs"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile = Join-Path $LogDir "tfjv_sync_$stamp.log"

function Log($msg) {
  $line = "[$(Get-Date -Format 'HH:mm:ss')] $msg"
  Write-Output $line
  Add-Content -Path $logFile -Value $line
}

Log "TFJV sync start"

# 既存TFJVプロセスがあれば停止
$existing = Get-Process TFJV -ErrorAction SilentlyContinue
if ($existing) {
  Log "  existing TFJV process found, stopping..."
  $existing | Stop-Process -Force
  Start-Sleep -Seconds 2
}

if (-not (Test-Path $TFJVExe)) {
  Log "ERROR: TFJV.EXE not found at $TFJVExe"
  exit 1
}

# CK_DATA の事前状態記録 (最新ファイルのmtime)
$beforeLatest = Get-ChildItem -Path $CKDataRoot -Recurse -Filter "*.DAT" -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($beforeLatest) {
  Log "  before: latest DAT = $($beforeLatest.Name) @ $($beforeLatest.LastWriteTime)"
}

# TFJV 起動
Log "  starting TFJV.EXE..."
$proc = Start-Process -FilePath $TFJVExe -PassThru -WindowStyle Minimized
Log "  pid=$($proc.Id), waiting $WaitSec sec for JV-Link sync..."

# 待機 (JV-Link 自動同期)
Start-Sleep -Seconds $WaitSec

# プロセス停止
Log "  stopping TFJV.EXE..."
Get-Process TFJV -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

# CK_DATA の事後状態記録
$afterLatest = Get-ChildItem -Path $CKDataRoot -Recurse -Filter "*.DAT" -ErrorAction SilentlyContinue |
               Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($afterLatest) {
  Log "  after:  latest DAT = $($afterLatest.Name) @ $($afterLatest.LastWriteTime)"
  if ($beforeLatest -and ($afterLatest.LastWriteTime -gt $beforeLatest.LastWriteTime)) {
    Log "  [OK] CK_DATA updated successfully"
  } elseif (-not $beforeLatest) {
    Log "  [OK] CK_DATA initialized"
  } else {
    Log "  [WARN] CK_DATA not updated (may be up-to-date already)"
  }
}

Log "TFJV sync done"
exit 0
