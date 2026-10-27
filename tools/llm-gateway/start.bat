@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title llm-gateway

rem 双击运行时窗口一闪而过，是因为脚本在"已在运行"这条路径上直接退出。
rem 现在所有退出路径都会 pause，任何提示都能看清。

where node >nul 2>nul
if errorlevel 1 (
    echo [错误] 找不到 node 命令。
    echo        请先安装 Node.js 18 及以上版本，并确保 node 在 PATH 里。
    echo        安装后新开一个命令行窗口再试。
    goto :done
)

if not exist "%~dp0config.json" (
    echo [错误] 缺少 config.json。
    echo        复制 config.example.json 为 config.json，填好 baseUrl 和 apiKey。
    goto :done
)

rem 额外参数原样传给 gateway.mjs，例如 start.bat --port 8788
node gateway.mjs --config "%~dp0config.json" %*

:done
echo.
echo 窗口保持打开，按任意键关闭。
pause >nul
