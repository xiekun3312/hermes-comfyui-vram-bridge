@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
python -B bridge.py serve
if errorlevel 1 pause
