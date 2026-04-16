# ======================================================================
#  Saturday preview を Windows Task Scheduler に登録
#  実行: 管理者 PowerShell で
#    powershell -ExecutionPolicy Bypass -File .\scripts\register_saturday_preview_task.ps1
#
#  スケジュール: 毎週金曜 19:00 (土曜枠順=金曜11時発表済、最新調教込み夕予想)
# ======================================================================
$TaskName   = "NorishikoAI_SaturdayPreview"
$BatPath    = "C:\Users\westr\norishiko_ai\scripts\saturday_preview.bat"
$WorkingDir = "C:\Users\westr\norishiko_ai"

if (-not (Test-Path $BatPath)) {
  Write-Error "Batch not found: $BatPath"
  exit 1
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Write-Host "既存タスク削除: $TaskName"
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# 旧タスクがあれば削除
if (Get-ScheduledTask -TaskName "NorishikoAI_FridayPreview" -ErrorAction SilentlyContinue) {
  Write-Host "旧 FridayPreview タスク削除"
  Unregister-ScheduledTask -TaskName "NorishikoAI_FridayPreview" -Confirm:$false
}

$Action = New-ScheduledTaskAction -Execute $BatPath -WorkingDirectory $WorkingDir
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At "19:00"

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
  -Description "norishiko_ai: 毎週金曜19:00 土曜レース予想 (training_import → publish_weekend --saturday)"

Write-Host "登録完了: $TaskName (毎週金曜 19:00)"
