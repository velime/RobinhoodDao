@echo off
chcp 65001 >nul
call "%~dp0_env.bat" || exit /b 1

echo === Сбор сообщений из Telegram-источников (без бота) ===
echo Картинки разбирает модель из .env (LLM_*), если она задана.
echo.
python -m bottom tg collect %*
pause
