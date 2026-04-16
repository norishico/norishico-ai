# ======================================================================
#  Monday preview を Windows Task Scheduler に登録
#  実行: 管理者 PowerShell で
#    powershell -ExecutionPolicy Bypass -File .\scripts\register_monday_preview_task.ps1
#
#  スケジュール: 毎週日曜 11:00 (月曜開催がある週のみ実質動作)
#  月曜レースが無ければ publish_weekend が空で終わるので副作用ゼロ
# ======================================================================
$TaskName   = "NorishikoAI_MondayPreview"
$BatPath    = "C:\Users\westr\norishiko_ai\scripts\monday_preview.bat"
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
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "11:00"

$Settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
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
  -Description "norishiko_ai: 毎週日曜11:00 月曜レース予想 (祝日月曜開催週のみ実質動作、無い週は空終了)"

Write-Host "登録完了: $TaskName (毎週日曜 11:00)"
