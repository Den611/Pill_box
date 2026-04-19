@echo off
title PillBox Server
chcp 65001 > nul
cd /d "%~dp0"
echo ========================================
echo   Starting PillBox Server...
echo ========================================

echo [INFO] Starting ngrok in a new window (port 8080)...
start "Ngrok" cmd /k "ngrok http 8080"

if not exist ".venv\Scripts\python.exe" goto global_python

echo [OK] Virtual environment found. Starting...
.\.venv\Scripts\python.exe main.py
goto end_script

:global_python
echo [WARNING] Virtual environment (.venv) not found!
echo Attempting to start via global python...
python main.py

:end_script
echo.
echo Server stopped!
pause