@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
echo === Проверка Telegram-источников: крипта или нет ===
echo Источники: channels\sources.json (публичные и приватные каналы, чаты).
echo.
where python >nul 2>nul
if errorlevel 1 (
  echo [X] Python не найден. Установи с https://www.python.org/downloads/ и поставь галочку "Add python.exe to PATH".
  pause
  exit /b 1
)
if not exist ".venv" python -m venv .venv
call ".venv\Scripts\activate.bat"
python -c "import telethon, dotenv" >nul 2>nul
if errorlevel 1 (
  echo Ставлю библиотеку Telegram...
  python -m pip install -q --upgrade pip
  python -m pip install -q telethon python-dotenv
  if errorlevel 1 (
    echo [X] Не удалось установить библиотеку. Пришли текст ошибки выше.
    pause
    exit /b 1
  )
)
python -m swarm.tg scan %*
if exist "channels\report.md" notepad "channels\report.md"
pause
