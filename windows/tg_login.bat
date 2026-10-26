@echo off
chcp 65001 >nul
call "%~dp0_env.bat" || exit /b 1

echo Вход в твой Telegram-аккаунт (нужен один раз).
echo.
python -m bottom tg login
pause
