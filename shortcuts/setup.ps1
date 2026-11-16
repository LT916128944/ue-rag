# 换机：找到 Python 3.12，建好 RAG/ue-rag/.venv，装上依赖。
# 不下载 UE，不写密钥。已有 .env 不动。30 分钟由人另记，这里不断言。
#
# 20261111（W1-P2）：setup 不再自己宣告成功。回执里的 ok 只是转述 check_all 的结论，
# 另记 check_all.json 的 sha256 与修改时间；摘要之外的东西（编辑器探活）只如实记，不参与 ok。
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Test-Py312([string]$Exe, [string[]]$Prefix) {
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) {
        return $false
    }
    & $Exe @Prefix -c "import sys; raise SystemExit(0 if sys.version_info[:2]==(3,12) else 1)"
    return $LASTEXITCODE -eq 0
}

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    if (Test-Py312 "py" @("-3.12")) {
        & py -3.12 -m venv (Join-Path $Root ".venv")
    } elseif (Test-Py312 "python" @()) {
        & python -m venv (Join-Path $Root ".venv")
    } else {
        throw "找不到 Python 3.12"
    }
}
if (-not (Test-Path $venvPy)) {
    throw "没有建出 .venv"
}

& $venvPy -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "pip install 失败"
}

$envFile = Join-Path $Root ".env"
$example = Join-Path $Root ".env.example"
if (-not (Test-Path $envFile)) {
    if (-not (Test-Path $example)) {
        throw "没有 .env.example"
    }
    Copy-Item $example $envFile
}

# 回执是先写的。只写能自己看到的事实（解释器路径、编辑器探活），ok 留给 check_all 转述。
$receiptPy = Join-Path $env:TEMP "ue_rag_setup_receipt.py"
$checkPy = Join-Path $env:TEMP "ue_rag_setup_after_check_all.py"
@'
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
editor_seen = False
editor = ""
try:
    from core.editor_launch import editor_exe
    exe = editor_exe()
    editor = str(exe)
    editor_seen = exe.is_file()
except Exception as exc:
    editor = type(exc).__name__
if sys.version_info[:2] != (3, 12):
    raise SystemExit("venv 不是 Python 3.12")
path = Path("data/harness/setup_receipt.json")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({
    "ok": None,
    "ok_source": "check_all",
    "check_all_sha256": None,
    "check_all_mtime": None,
    "python": "%d.%d.%d" % sys.version_info[:3],
    "venv": sys.executable,
    "editor_seen": editor_seen,
    "editor": editor,
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
'@ | Set-Content -Encoding utf8 $receiptPy
& $venvPy $receiptPy
$receiptCode = $LASTEXITCODE
Remove-Item $receiptPy -ErrorAction SilentlyContinue
if ($receiptCode -ne 0) {
    throw "没有写上换机回执"
}

# 结论只从 check_all 来。check_all 退 0 才算通过；没跑起来就是没通过，不补一个 true。
& $venvPy eval/check_all.py
$checkAllCode = $LASTEXITCODE

@'
import hashlib
import json
import sys
from pathlib import Path

summary = Path("data/harness/check_all.json")
receipt = Path("data/harness/setup_receipt.json")
passed = len(sys.argv) > 1 and sys.argv[1] == "0"
recorded = None
stamp = None
if summary.is_file():
    recorded = hashlib.sha256(summary.read_bytes()).hexdigest()
    stamp = summary.stat().st_mtime
data = json.loads(receipt.read_text(encoding="utf-8"))
data["ok"] = bool(passed and recorded is not None)
data["ok_source"] = "check_all"
data["check_all_sha256"] = recorded
data["check_all_mtime"] = stamp
data["check_all_exit"] = int(sys.argv[1]) if len(sys.argv) > 1 else None
receipt.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("setup_receipt ok=%s check_all_exit=%s" % (data["ok"], data["check_all_exit"]))
'@ | Set-Content -Encoding utf8 $checkPy
& $venvPy $checkPy $checkAllCode
$receiptCode = $LASTEXITCODE
Remove-Item $checkPy -ErrorAction SilentlyContinue
if ($receiptCode -ne 0) {
    throw "没有写上换机回执的结论"
}

Write-Output "下一步："
Write-Output "python eval/check_all.py"
Write-Output "shortcuts/ingest.bat"
Write-Output "AIAgentProject/start.bat"
