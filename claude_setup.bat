@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
where python >nul 2>nul
if errorlevel 1 (
  echo [X] Python не найден. Установи с https://www.python.org/downloads/ и поставь галочку "Add python.exe to PATH".
  pause
  exit /b 1
)
if not exist ".venv" python -m venv .venv
call ".venv\Scripts\activate.bat"
python -c "import httpx, aiogram, ccxt, openai, anthropic, telethon, dotenv, ddgs" >nul 2>nul
if errorlevel 1 (
  echo Доустанавливаю библиотеки, это займёт пару минут...
  python -m pip install -q --upgrade pip
  python -m pip install -q -e .
  if errorlevel 1 (
    echo [X] Не удалось установить библиотеки. Пришли текст ошибки выше.
    pause
    exit /b 1
  )
)
if not exist ".env" copy ".env.example" ".env" >nul
where claude >nul 2>nul
if errorlevel 1 if not exist "%USERPROFILE%\.local\bin\claude.exe" (
  echo Устанавливаю Claude Code...
  where git >nul 2>nul
  if errorlevel 1 echo [!] Claude Code на Windows нужен Git: https://git-scm.com/download/win - поставь его, если установка ниже попросит.
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
)
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
python -m bottom.claude_code setup
echo.
echo Дальше:
echo  1. Запусти claude_chat.bat - откроется Claude Code, войди своим аккаунтом Claude (подписка, без API-ключа).
echo  2. Чтобы Telegram-бот отвечал через Claude Code: в .env поставь LLM_PROVIDER=claude_code и запусти start.bat.
pause
