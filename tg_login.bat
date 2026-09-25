@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
call ".venv\Scripts\activate.bat"
echo Вход в твой Telegram-аккаунт (нужен один раз).
echo Введи номер телефона в международном формате, например +380XXXXXXXXX.
echo Код придёт в приложение Telegram (чат "Telegram"). Если включён облачный пароль - введи и его.
echo.
python -m swarm.tg login
pause
