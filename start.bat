@echo off
title PillBox Server
cd /d "%~dp0"
echo ========================================
echo   Запуск сервера PillBox...
echo ========================================

echo [INFO] Запуск ngrok у новому вікні (порт 8080)...
start "Ngrok" cmd /k "ngrok http 8080"

:: Перевіряємо чи існує папка віртуального оточення
if exist ".venv\Scripts\python.exe" (
    echo [OK] Знайдено віртуальне оточення. Запускаємо...
    .\.venv\Scripts\python.exe main.py
) else (
    echo [WARNING] Віртуальне оточення (.venv) не знайдено!
    echo Спроба запуску через глобальний python...
    python main.py
)

pause
