@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv" (
  echo Сначала запусти install.bat
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"
echo Запускаю бота. Чтобы остановить - закрой это окно или нажми Ctrl+C.
echo.
python -m swarm
echo.
echo Бот остановлен. Если выше есть ошибка - пришли её текст.
pause
