@echo off
chcp 65001 >nul
call "%~dp0_env.bat" || exit /b 1

echo [OK] Bottom установлен.
echo Сейчас откроется файл настроек .env в Блокноте.
echo Впиши ключи (см. docs\windows.md), сохрани Ctrl+S и закрой Блокнот.
pause
notepad ".env"
echo Теперь запусти check.bat, чтобы проверить настройки и источники.
pause
