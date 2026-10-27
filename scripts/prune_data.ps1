# scripts/prune_data.ps1
# 定时清理 ue-rag/data 下的运行残留，默认保留最近 3 天。
# 由计划任务每天 0 点调起，也可手工跑：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\prune_data.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\prune_data.ps1 -DryRun
param(
    [int]$Days = 3,
    [string]$Roots = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Script = Join-Path $RepoRoot "scripts\prune_data.py"
$LogDir = Join-Path $RepoRoot "data\logs"
$LogFile = Join-Path $LogDir "prune_data.log"

if (-not (Test-Path $Python)) {
    $Python = "python"
}
if (-not (Test-Path $Script)) {
    throw "prune_data.py not found: $Script"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$argv = @($Script, "--days", "$Days")
if ($Roots) { $argv += @("--roots", $Roots) }
if ($DryRun) { $argv += "--dry-run" }

# 输出先收成字符串，统一按 UTF-8 落盘；直接管道到文件会被 PowerShell 写成 UTF-16。
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$body = (& $Python @argv 2>&1 | Out-String)
$code = $LASTEXITCODE

Write-Host $body.TrimEnd()

$entry = @(
    "=== $stamp start ===",
    $body.TrimEnd(),
    "=== $stamp exit $code ===",
    ""
) -join "`r`n"
Add-Content -Path $LogFile -Value $entry -Encoding utf8

# 日志自身也轮转，避免长成第二个堆积源
if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 1MB) {
    Move-Item -Force $LogFile "$LogFile.1"
}

exit $code
