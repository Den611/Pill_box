@echo off
chcp 65001
title PillBox Server
cd /d "%~dp0"
echo ========================================
echo   Запуск сервера PillBox...
echo ========================================

echo [INFO] Запуск ngrok у новому вікні (порт 8080)...
start "Ngrok" cmd /k "ngrok http 8080"

if not exist ".venv\Scripts\python.exe" goto global_python

echo [OK] Знайдено віртуальне оточення. Запускаємо...
.\.venv\Scripts\python.exe main.py
goto end_script

:global_python
echo [WARNING] Віртуальне оточення (.venv) не знайдено!
echo Спроба запуску через глобальний python...
python main.py

:end_script
echo.
echo Робота сервера завершена або сталася помилка!
pause
