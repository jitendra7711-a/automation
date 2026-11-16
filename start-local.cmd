@echo off
setlocal
set "LOCAL_PREVIEW=1"
"%~dp0.venv\Scripts\python.exe" "%~dp0app.py"
