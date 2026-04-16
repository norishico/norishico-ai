# ======================================================================
#  Sunday preview を Windows Task Scheduler に登録
#  実行: 管理者 PowerShell で
#    powershell -ExecutionPolicy Bypass -File .\scripts\register_sunday_preview_task.ps1
#
#  スケジュール: 毎週土曜 11:00 (日曜枠順=土曜09時発表済)
# ======================================================================
$TaskName   = "NorishikoAI_SundayPreview"
$BatPath    = "C:\Users\westr\norishiko_ai\scripts\sunday_preview.bat"
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
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At "11:00"

$Settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
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
  -Description "norishiko_ai: 毎週土曜11:00 日曜レース予想 (training_import → publish_weekend --sunday)"

Write-Host "登録完了: $TaskName (毎週土曜 11:00)"
