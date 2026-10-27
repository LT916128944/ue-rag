@echo off
chcp 65001 >nul
cd /d E:\UEProject\AIWork\RAG\ue-rag
echo ============================================
echo  DeepSeek Harness 预检
echo  工作区: E:\UEProject\AIWork
echo ============================================
.venv\Scripts\python.exe shortcuts\dsh_preflight.py
echo.
echo 下一步:
echo   1. 打开 DeepSeek Harness，工作区选 E:\UEProject\AIWork
echo   2. 确认 /permission 为 workspace-write
echo   3. 检查: python eval/check_dsh_config.py
echo   4. 检查: python eval/check_dsh_bridge.py
echo ============================================
pause
