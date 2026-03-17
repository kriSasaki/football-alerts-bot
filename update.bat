@echo off
chcp 65001 >nul
echo ═══════════════════════════════════════════
echo   Sports Alerts Bot — Обновление
echo ═══════════════════════════════════════════
echo.

cd /d "%~dp0"

echo [1/4] Останавливаю бота (если запущен)...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq *bot.py*" 2>nul
timeout /t 2 /nobreak >nul

echo [2/4] Создаю бэкап данных...
if not exist backups mkdir backups
set BACKUP_DIR=backups\%date:~6,4%-%date:~3,2%-%date:~0,2%_%time:~0,2%-%time:~3,2%
set BACKUP_DIR=%BACKUP_DIR: =0%
mkdir "%BACKUP_DIR%" 2>nul
if exist alerts.db copy /Y alerts.db "%BACKUP_DIR%\alerts.db" >nul
if exist approved_users.json copy /Y approved_users.json "%BACKUP_DIR%\approved_users.json" >nul
if exist .env copy /Y .env "%BACKUP_DIR%\.env" >nul
echo    Бэкап сохранён: %BACKUP_DIR%

echo [3/4] Загружаю обновления из GitHub...
REM Сбрасываем любые локальные конфликты/изменения в коде
REM Данные (.env, alerts.db, approved_users.json) защищены .gitignore
git reset --hard HEAD 2>nul
git clean -fd --exclude=.env --exclude=alerts.db --exclude=alerts.db-journal --exclude=approved_users.json --exclude=backups 2>nul
git pull origin main
if errorlevel 1 (
    echo.
    echo [!] Git pull не удался. Пробую полный сброс...
    git fetch origin main
    git reset --hard origin/main
)

echo [4/4] Обновляю зависимости...
pip install -r requirements.txt --break-system-packages -q 2>nul
pip install -r requirements.txt -q 2>nul

echo.
echo ═══════════════════════════════════════════
echo   Готово! Данные сохранены.
echo   Запусти бота: python bot.py
echo ═══════════════════════════════════════════
pause
