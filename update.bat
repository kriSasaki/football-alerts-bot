@echo off
chcp 65001 >nul
echo ============================================
echo   Sports Alerts Bot - Update
echo ============================================
echo.

cd /d "%~dp0"

echo [1/4] Stopping bot if it is running...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq *bot.py*" 2>nul
timeout /t 2 /nobreak >nul

echo [2/4] Backing up data...
if not exist backups mkdir backups
set BACKUP_DIR=backups\%date:~6,4%-%date:~3,2%-%date:~0,2%_%time:~0,2%-%time:~3,2%
set BACKUP_DIR=%BACKUP_DIR: =0%
mkdir "%BACKUP_DIR%" 2>nul
if exist alerts.db copy /Y alerts.db "%BACKUP_DIR%\alerts.db" >nul
if exist approved_users.json copy /Y approved_users.json "%BACKUP_DIR%\approved_users.json" >nul
if exist .env copy /Y .env "%BACKUP_DIR%\.env" >nul
echo    Backup saved to: %BACKUP_DIR%

echo [3/4] Pulling updates from Git...
git reset --hard HEAD 2>nul
git clean -fd --exclude=.env --exclude=alerts.db --exclude=alerts.db-journal --exclude=alerts.db-wal --exclude=approved_users.json --exclude=backups 2>nul
git pull origin main
if errorlevel 1 (
    echo.
    echo [!] Git pull failed. Trying full reset...
    git fetch origin main
    git reset --hard origin/main
)

if exist "%BACKUP_DIR%\.env" (
    copy /Y "%BACKUP_DIR%\.env" .env >nul
    echo    .env restored from backup
)

echo [4/4] Updating dependencies...
pip install -r requirements.txt --break-system-packages -q 2>nul
pip install -r requirements.txt -q 2>nul

echo.
echo ============================================
echo   Done. Your data is preserved.
echo   Start the bot with: python bot.py
echo ============================================
pause
