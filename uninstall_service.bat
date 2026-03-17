@echo off
chcp 65001 >nul
set SERVICE_NAME=SportsAlertsBot

echo Останавливаю сервис %SERVICE_NAME%...
nssm.exe stop %SERVICE_NAME% 2>nul
timeout /t 3 /nobreak >nul

echo Удаляю сервис...
nssm.exe remove %SERVICE_NAME% confirm

echo.
echo Сервис удалён. Бот больше не запускается автоматически.
echo Можно запустить вручную: python bot.py
pause
