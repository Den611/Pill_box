@echo off
title PillBox Server
cd /d "%~dp0"
echo ========================================
echo ========================================

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
