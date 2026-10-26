@echo off
chcp 65001 >nul
call "%~dp0_env.bat" || exit /b 1

echo Запускаю бота. Чтобы остановить - закрой это окно или нажми Ctrl+C.
echo.
python -m bottom
echo.
echo Бот остановлен. Если выше есть ошибка - пришли её текст.
pause
