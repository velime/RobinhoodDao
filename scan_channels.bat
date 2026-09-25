@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
echo === Разовая проверка Telegram-каналов ===
echo Проверит каналы из channels\all.txt и твои подписки, оставит только крипто.
echo.
where python >nul 2>nul
if errorlevel 1 (
  echo [X] Python не найден.
  echo     Установи Python с https://www.python.org/downloads/
  echo     В установщике ОБЯЗАТЕЛЬНО поставь галочку "Add python.exe to PATH".
  echo     Потом закрой это окно и запусти scan_channels.bat ещё раз.
  pause
  exit /b 1
)
if not exist ".venv" (
  echo Готовлю окружение, это один раз...
  python -m venv .venv
)
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
echo.
python -m swarm.tg scan --include-dialogs %*
if exist "channels\report.md" notepad "channels\report.md"
pause
