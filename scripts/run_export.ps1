# run_export.ps1
$UE_EDITOR = "D:\UEEngline\UE_5.82\UE_5.8\Engine\Binaries\Win64\UnrealEditor-Cmd.exe"
$PROJECT   = "E:\UEProject\AIWork\AIAgentProject\AIAgentProject.uproject"
$SCRIPT    = "E:\UEProject\AIWork\RAG\ue-rag\scripts\export_asset_metadata.py"

if (-not (Test-Path $UE_EDITOR)) { throw "UnrealEditor-Cmd.exe not found: $UE_EDITOR" }
if (-not (Test-Path $PROJECT))   { throw "Project not found: $PROJECT" }
if (-not (Test-Path $SCRIPT))    { throw "Script not found: $SCRIPT" }

& $UE_EDITOR $PROJECT `
  -ExecutePythonScript="$SCRIPT" `
  -unattended -nopause -nosplash -nullrhi -stdout

$code = $LASTEXITCODE
Write-Host "UnrealEditor-Cmd exit code: $code"
exit $code