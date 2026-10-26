@echo off
chcp 65001 >nul
call "%~dp0_env.bat" || exit /b 1

where claude >nul 2>nul
if errorlevel 1 if not exist "%USERPROFILE%\.local\bin\claude.exe" (
  echo Устанавливаю Claude Code...
  where git >nul 2>nul
  if errorlevel 1 echo [!] Claude Code на Windows нужен Git: https://git-scm.com/download/win - поставь его, если установка ниже попросит.
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
)
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
python -m bottom claude setup
echo.
echo Дальше:
echo  1. Запусти claude_chat.bat - откроется Claude Code, войди своим аккаунтом Claude (подписка, без API-ключа).
echo  2. Чтобы Telegram-бот отвечал через Claude Code: в .env поставь LLM_PROVIDER=claude_code и запусти start.bat.
pause
