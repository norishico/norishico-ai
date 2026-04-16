# ======================================================================
#  週次実運用モニタリングタスクを Windows Task Scheduler に登録
#  実行: 管理者 PowerShell で以下を実行
#    powershell -ExecutionPolicy Bypass -File .\scripts\register_monitoring_task.ps1
#
#  スケジュール: 毎週月曜 08:00 (先週末のレース結果を反映後)
# ======================================================================
$TaskName   = "NorishikoAI_WeeklyMonitoring"
$BatPath    = "C:\Users\westr\norishiko_ai\scripts\weekly_monitoring.bat"
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

$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "08:00"

$Settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
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
  -Description "norishiko_ai 週次実運用モニタリング: monitor_actual → 判定 → alert → dashboard"

Write-Host "登録完了: $TaskName"
Write-Host "実行日時: 毎週月曜 08:00"
Write-Host "手動実行: Start-ScheduledTask -TaskName $TaskName"
Write-Host "状態確認: Get-ScheduledTaskInfo -TaskName $TaskName"
