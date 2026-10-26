@echo off
rem Общая подготовка для всех .bat: папка проекта, Python, .venv, библиотеки, .env.
rem Вызывается из других файлов: call "%~dp0_env.bat" || exit /b 1
chcp 65001 >nul
cd /d "%~dp0.."
set PYTHONUTF8=1
where python >nul 2>nul
if errorlevel 1 (
  echo [X] Python не найден. Установи Python 3.12 с https://www.python.org/downloads/
  echo     В установщике ОБЯЗАТЕЛЬНО поставь галочку "Add python.exe to PATH", потом запусти этот файл снова.
  pause
  exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo [X] Нужен Python 3.11 или новее. Установи свежий с https://www.python.org/downloads/
  pause
  exit /b 1
)
if not exist ".venv" python -m venv .venv
call ".venv\Scripts\activate.bat"
python -c "import bottom, httpx, aiogram, ccxt, openai, anthropic, telethon, dotenv, ddgs" >nul 2>nul
if errorlevel 1 (
  echo Ставлю библиотеки, это займёт пару минут...
  python -m pip install -q --upgrade pip
  python -m pip install -q -e .
  if errorlevel 1 (
    echo [X] Не удалось установить библиотеки. Пришли текст ошибки выше.
    pause
    exit /b 1
  )
)
if not exist ".env" copy ".env.example" ".env" >nul
exit /b 0
