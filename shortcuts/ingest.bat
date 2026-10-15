@echo off
chcp 65001 >nul
setlocal EnableExtensions

for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "PY=%ROOT%/.venv/Scripts/python.exe"
set "ERR=0"

cd /d "%ROOT%"

if exist "%ROOT%\.env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ROOT%\.env") do (
    if not "%%A"=="" if not "%%B"=="" set "%%A=%%B"
  )
)

set "MODE=%~1"
set "NOPAUSE="
if /I "%~2"=="nopause" set "NOPAUSE=1"
if not "%MODE%"=="" goto dispatch

echo cpp   = C++ 扫描并入库
echo asset = 蓝图/资产扫描并入库，会启动无界面编辑器
echo mcp   = 重新导出工具索引并入库，会启动无界面编辑器
set /p MODE=输入模式: 

:dispatch
if /I "%MODE%"=="cpp" goto cpp
if /I "%MODE%"=="asset" goto asset
if /I "%MODE%"=="mcp" goto mcp

echo 未知模式: "%MODE%"
echo 只能是 cpp、asset、mcp
set "ERR=1"
goto done

:cpp
if not exist "%PY%" (
  echo 找不到 Python: %PY%
  set "ERR=1"
  goto done
)
echo [cpp] export_cpp_metadata.py
"%PY%" "%ROOT%/pipelines/export_cpp_metadata.py"
if errorlevel 1 (
  echo C++ 导出失败，未入库。
  set "ERR=1"
  goto done
)
echo [cpp] ingest_assets.py
"%PY%" "%ROOT%/pipelines/ingest_assets.py"
if errorlevel 1 set "ERR=1"
goto done

:asset
call :paths
if not exist "%UE_EDITOR%" (
  echo 找不到 UnrealEditor-Cmd.exe: %UE_EDITOR%
  set "ERR=1"
  goto done
)
if not exist "%PROJECT%" (
  echo 找不到工程: %PROJECT%
  set "ERR=1"
  goto done
)
if not exist "%PY%" (
  echo 找不到 Python: %PY%
  set "ERR=1"
  goto done
)
echo [asset] export_asset_metadata.py
echo 若本工程的编辑器已打开，请先关闭，否则导出会失败。
"%UE_EDITOR%" "%PROJECT%" -ExecutePythonScript="%ROOT%/pipelines/export_asset_metadata.py" -unattended -nopause -nosplash -nullrhi -stdout
if errorlevel 1 (
  echo 编辑器导出失败，未入库。
  set "ERR=1"
  goto done
)
echo [asset] ingest_assets.py
"%PY%" "%ROOT%/pipelines/ingest_assets.py"
if errorlevel 1 set "ERR=1"
goto done

:mcp
call :paths
if not exist "%UE_EDITOR%" (
  echo 找不到 UnrealEditor-Cmd.exe: %UE_EDITOR%
  set "ERR=1"
  goto done
)
if not exist "%REGEN%" (
  echo 找不到 regenerate_index.ps1: %REGEN%
  set "ERR=1"
  goto done
)
if not exist "%PY%" (
  echo 找不到 Python: %PY%
  set "ERR=1"
  goto done
)
echo [mcp] regenerate_index.ps1
echo 若本工程的编辑器已打开，请先关闭，否则导出会失败。
powershell -NoProfile -ExecutionPolicy Bypass -File "%REGEN%"
if errorlevel 1 (
  echo 工具索引导出失败，未入库。
  set "ERR=1"
  goto done
)
if not exist "%SKILL_INDEX%" (
  echo 未写出技能索引: %SKILL_INDEX%
  set "ERR=1"
  goto done
)
echo [mcp] 复制到 data/ue_mcp_index.json
copy /Y "%SKILL_INDEX%" "%ROOT%/data/ue_mcp_index.json" >nul
if errorlevel 1 (
  echo 复制索引失败。
  set "ERR=1"
  goto done
)
echo [mcp] ingest_tools.py
"%PY%" "%ROOT%/pipelines/ingest_tools.py"
if errorlevel 1 set "ERR=1"
goto done

:paths
"%PY%" -c "from config import UE_EDITOR_CMD; print(UE_EDITOR_CMD)" > "%TEMP%\ue_rag_editor.txt"
set /p UE_EDITOR=<"%TEMP%\ue_rag_editor.txt"
"%PY%" -c "from config import UE_PROJECT; print(UE_PROJECT)" > "%TEMP%\ue_rag_project.txt"
set /p PROJECT=<"%TEMP%\ue_rag_project.txt"
"%PY%" -c "from config import REGEN_INDEX_PS1; print(REGEN_INDEX_PS1)" > "%TEMP%\ue_rag_regen.txt"
set /p REGEN=<"%TEMP%\ue_rag_regen.txt"
"%PY%" -c "from config import SKILL_MCP_INDEX; print(SKILL_MCP_INDEX)" > "%TEMP%\ue_rag_index.txt"
set /p SKILL_INDEX=<"%TEMP%\ue_rag_index.txt"
exit /b 0

:done
echo.
if "%ERR%"=="0" (
  echo 完成。
) else (
  echo 失败。
)
if not "%NOPAUSE%"=="1" pause
endlocal & exit /b %ERR%
