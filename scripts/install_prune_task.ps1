# scripts/install_prune_task.ps1
# 注册/卸载「每天 0 点清理 data/ 残留」的计划任务。
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_prune_task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_prune_task.ps1 -Days 5 -At 03:30
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_prune_task.ps1 -Uninstall
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_prune_task.ps1 -Status
param(
    [int]$Days = 3,
    [string]$At = "00:00",
    [string]$TaskName = "ue-rag-prune-data",
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $RepoRoot "scripts\prune_data.ps1"

if (-not (Test-Path $Runner)) {
    throw "prune_data.ps1 not found: $Runner"
}

if ($Status) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "任务 $TaskName 未注册"
        exit 1
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "任务：$TaskName"
    Write-Host "状态：$($task.State)"
    Write-Host "上次运行：$($info.LastRunTime)（结果 $($info.LastTaskResult)）"
    Write-Host "下次运行：$($info.NextRunTime)"
    exit 0
}

if ($Uninstall) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已卸载 $TaskName"
    } else {
        Write-Host "$TaskName 本来就不存在"
    }
    exit 0
}

if ($At -notmatch '^\d{1,2}:\d{2}$') {
    throw "时间格式应为 HH:mm，收到：$At"
}
$when = [datetime]::ParseExact($At, "H:mm", $null)

$argLine = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Days {1}' -f $Runner, $Days
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argLine -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $when
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description "ue-rag: 每天 $At 清理 data/ 下 $Days 天前的运行残留" -Force | Out-Null

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "已注册 $TaskName"
Write-Host "每天 $At 运行，保留 $Days 天，下次运行：$($info.NextRunTime)"
Write-Host "日志：$(Join-Path $RepoRoot 'data\logs\prune_data.log')"
