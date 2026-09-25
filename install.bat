@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка Swarm ===
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo [X] Python не найден.
  echo     Установи Python 3.12 с https://www.python.org/downloads/
  echo     В установщике ОБЯЗАТЕЛЬНО поставь галочку "Add python.exe to PATH".
  echo     Потом закрой это окно и запусти install.bat ещё раз.
  pause
  exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo [X] Нужен Python 3.11 или новее. Установи свежий с https://www.python.org/downloads/
  pause
  exit /b 1
)
if not exist ".venv" (
  echo Создаю окружение...
  python -m venv .venv
)
call ".venv\Scripts\activate.bat"
echo Ставлю библиотеки, это займёт пару минут...
python -m pip install --upgrade pip -q
pip install -e . -q
if errorlevel 1 (
  echo [X] Не удалось установить библиотеки. Пришли текст ошибки выше.
  pause
  exit /b 1
)
if not exist ".env" copy ".env.example" ".env" >nul
echo.
echo [OK] Установлено.
echo Сейчас откроется файл настроек .env в Блокноте.
echo Впиши ключи (см. docs\windows.md), сохрани Ctrl+S и закрой Блокнот.
pause
notepad ".env"
echo Теперь запусти check.bat, чтобы проверить настройки.
pause
