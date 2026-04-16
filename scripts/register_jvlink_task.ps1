# ======================================================================
#  JV-Link Phase 2 並行取得タスクを Windows Task Scheduler に登録
#  実行: 管理者 PowerShell で以下を実行
#    powershell -ExecutionPolicy Bypass -File .\scripts\register_jvlink_task.ps1
#
#  スケジュール: 毎日 04:30 と 23:30 (JRA-VAN 定時更新後)
#  変更したい時刻は $TriggerTimes を編集
# ======================================================================
$TaskName   = "NorishikoAI_JVLinkParallel"
$BatPath    = "C:\Users\westr\norishiko_ai\scripts\jvlink_parallel_fetch.bat"
$WorkingDir = "C:\Users\westr\norishiko_ai"

if (-not (Test-Path $BatPath)) {
  Write-Error "Batch not found: $BatPath"
  exit 1
}

# 既存タスクがあれば削除
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Write-Host "既存タスク削除: $TaskName"
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$Action = New-ScheduledTaskAction -Execute $BatPath -WorkingDirectory $WorkingDir

$TriggerTimes = @("04:30", "23:30")
$Triggers = foreach ($t in $TriggerTimes) {
  New-ScheduledTaskTrigger -Daily -At $t
}

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
  -Trigger $Triggers `
  -Settings $Settings `
  -Principal $Principal `
  -Description "norishiko_ai Phase 2: JV-Link 差分取得 → keiba_staging.db 更新 (並行検証用、本番DBには触らない)"

Write-Host "登録完了: $TaskName"
Write-Host "実行時刻: $($TriggerTimes -join ', ')"
Write-Host "手動実行: Start-ScheduledTask -TaskName $TaskName"
Write-Host "状態確認: Get-ScheduledTaskInfo -TaskName $TaskName"
