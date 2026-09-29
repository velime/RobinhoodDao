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
echo Запускаю бота. Чтобы остановить - закрой это окно или нажми Ctrl+C.
echo.
python -m bottom
echo.
echo Бот остановлен. Если выше есть ошибка - пришли её текст.
pause
