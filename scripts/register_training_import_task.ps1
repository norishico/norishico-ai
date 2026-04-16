# ======================================================================
#  TFJV調教データ自動インポートを Windows Task Scheduler に登録
#  実行: 管理者 PowerShell で
#    powershell -ExecutionPolicy Bypass -File .\scripts\register_training_import_task.ps1
#
#  スケジュール: 毎日 20:00 (TFJVがJV-Linkで取得した後のインポート)
#  特に金曜20時はFridayPreview(21時)の直前で最新調教が揃う
# ======================================================================
$TaskName   = "NorishikoAI_TrainingImport"
$BatPath    = "C:\Users\westr\norishiko_ai\scripts\training_import.bat"
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
$Trigger = New-ScheduledTaskTrigger -Daily -At "20:00"

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
  -Description "norishiko_ai: 毎日20時 TFJV CK_DATA → training テーブル自動インポート (金曜21時のFridayPreview前に最新調教を反映)"

Write-Host "登録完了: $TaskName (毎日 20:00)"
Write-Host "手動実行: Start-ScheduledTask -TaskName $TaskName"
