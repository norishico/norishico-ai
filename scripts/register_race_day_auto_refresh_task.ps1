# ======================================================================
#  Race-day auto_refresh starter を Windows Task Scheduler に登録
#  実行: 管理者 PowerShell で
#    powershell -ExecutionPolicy Bypass -File .\scripts\register_race_day_auto_refresh_task.ps1
#
#  スケジュール: 毎日 09:00
#  - bat内で曜日判定 (土日月のみ起動、他は即終了)
#  - 今日のレースが weekend_predictions.json に無ければ即終了
#  - 09:00 起動 Step1: --once で1回強制オッズチェック+git push (朝の最新スナップショット)
#  - Step2: auto_refresh.py 通常ループ (発走10分前トリガー+±20%ロック)
# ======================================================================
$TaskName   = "NorishikoAI_RaceDayAutoRefresh"
$BatPath    = "C:\Users\westr\norishiko_ai\scripts\race_day_auto_refresh.bat"
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
$Trigger = New-ScheduledTaskTrigger -Daily -At "09:00"

$Settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
  -MultipleInstances IgnoreNew

$Principal = New-ScheduledTaskPrincipal `
  -UserId "$env:USERDOMAIN\$env:USERNAME" `
  -LogonType Interactive `
  -RunLevel Limited

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $Action `
  -Trigger $Trigger `
  -Settings $Settings `
  -Principal $Principal `
  -Description "norishiko_ai: 毎朝09:00 開催日判定→auto_refresh強制1回チェック+通常ループ起動 (土日月祝、他曜日は即終了)"

Write-Host "登録完了: $TaskName (毎日 09:00, bat内で開催日判定)"
