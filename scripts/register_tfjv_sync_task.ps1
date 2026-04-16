# ======================================================================
#  TFJV自動同期を Windows Task Scheduler に登録
#  実行: 管理者PowerShell で
#    powershell -ExecutionPolicy Bypass -File .\scripts\register_tfjv_sync_task.ps1
#
#  スケジュール: 毎朝 06:30 と 19:30 (朝晩2回、調教データの新着を捕捉)
#  動作: TFJV.EXE 起動 → 90秒待機(JV-Link同期) → 強制終了
#  後続: 20:00 TrainingImport が CK_DATA から DB に取り込み
# ======================================================================
$TaskName   = "NorishikoAI_TFJVSync"
$BatPath    = "C:\Users\westr\norishiko_ai\scripts\tfjv_sync.bat"
$WorkingDir = "C:\Users\westr\norishiko_ai"

if (-not (Test-Path $BatPath)) {
  Write-Error "Batch not found: $BatPath"
  exit 1
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Write-Host "既存タスク削除: $TaskName"
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$Action = New-ScheduledTaskAction -Execute $BatPath -WorkingDirectory $WorkingDir

# 朝晩2回
$TriggerTimes = @("06:30", "19:30")
$Triggers = foreach ($t in $TriggerTimes) {
  New-ScheduledTaskTrigger -Daily -At $t
}

$Settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
  -MultipleInstances IgnoreNew

$Principal = New-ScheduledTaskPrincipal `
  -UserId "$env:USERDOMAIN\$env:USERNAME" `
  -LogonType Interactive `
  -RunLevel Limited

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $Action `
  -Trigger $Triggers `
  -Settings $Settings `
  -Principal $Principal `
  -Description "norishiko_ai: 毎日06:30/19:30 TFJV起動→JV-Link自動同期→終了 (調教DATを最新化)"

Write-Host "登録完了: $TaskName"
Write-Host "実行時刻: $($TriggerTimes -join ', ')"
Write-Host "手動実行: Start-ScheduledTask -TaskName $TaskName"
Write-Host "ログ: C:\Users\westr\norishiko_ai\logs\tfjv_sync_YYYYMMDD_HHMMSS.log"
