@echo off
set "EXTRA="
if /I "%~1"=="nopause" set "EXTRA=nopause"
call "%~dp0ingest.bat" cpp %EXTRA%
