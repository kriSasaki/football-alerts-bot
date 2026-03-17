@echo off
chcp 65001 >nul
echo ═══════════════════════════════════════════
echo   Sports Alerts Bot — Установка сервиса
echo ═══════════════════════════════════════════
echo.

cd /d "%~dp0"

REM Проверяем наличие NSSM
if not exist nssm.exe (
    echo [!] nssm.exe не найден в текущей папке.
    echo.
    echo Скачай NSSM отсюда: https://nssm.cc/download
    echo Распакуй и положи nssm.exe в: %~dp0
    echo Затем запусти этот скрипт снова.
    echo.
    pause
    exit /b 1
)

set SERVICE_NAME=SportsAlertsBot
set BOT_DIR=%~dp0
set PYTHON_PATH=python

REM Ищем python.exe
where python >nul 2>&1
if errorlevel 1 (
    echo [!] Python не найден в PATH.
    echo Укажи полный путь к python.exe:
    set /p PYTHON_PATH="Python path: "
)

echo.
echo Конфигурация:
echo   Сервис:  %SERVICE_NAME%
echo   Папка:   %BOT_DIR%
echo   Python:  %PYTHON_PATH%
echo.

REM Удаляем старый сервис если есть
nssm.exe stop %SERVICE_NAME% >nul 2>&1
nssm.exe remove %SERVICE_NAME% confirm >nul 2>&1

REM Устанавливаем сервис
echo [1/4] Устанавливаю сервис...
nssm.exe install %SERVICE_NAME% "%PYTHON_PATH%" "bot.py"

echo [2/4] Настраиваю параметры...
REM Рабочая директория
nssm.exe set %SERVICE_NAME% AppDirectory "%BOT_DIR%"
REM Описание
nssm.exe set %SERVICE_NAME% Description "Telegram Sports Alerts Bot v2 (SofaScore)"
REM Автозапуск
nssm.exe set %SERVICE_NAME% Start SERVICE_AUTO_START
REM Перезапуск при падении (через 10 сек)
nssm.exe set %SERVICE_NAME% AppRestartDelay 10000
REM Перенаправляем stdout/stderr в файл (дополнительно к логам бота)
nssm.exe set %SERVICE_NAME% AppStdout "%BOT_DIR%logs\service_stdout.log"
nssm.exe set %SERVICE_NAME% AppStderr "%BOT_DIR%logs\service_stderr.log"
REM Ротация логов сервиса при 5MB
nssm.exe set %SERVICE_NAME% AppStdoutCreationDisposition 4
nssm.exe set %SERVICE_NAME% AppStderrCreationDisposition 4
nssm.exe set %SERVICE_NAME% AppRotateFiles 1
nssm.exe set %SERVICE_NAME% AppRotateBytes 5242880

echo [3/4] Создаю папку логов...
if not exist logs mkdir logs

echo [4/4] Запускаю сервис...
nssm.exe start %SERVICE_NAME%

echo.
echo ═══════════════════════════════════════════
echo   Готово! Бот работает как сервис.
echo.
echo   Управление:
echo     nssm start %SERVICE_NAME%
echo     nssm stop %SERVICE_NAME%
echo     nssm restart %SERVICE_NAME%
echo     nssm status %SERVICE_NAME%
echo     nssm remove %SERVICE_NAME% confirm
echo.
echo   Логи:
echo     logs\bot.log       — полный лог
echo     logs\errors.log    — только ошибки
echo.
echo   Бот автоматически перезапустится
echo   при падении и при перезагрузке Windows.
echo ═══════════════════════════════════════════
pause
