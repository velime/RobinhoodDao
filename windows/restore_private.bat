@echo off
chcp 65001 >nul
call "%~dp0_env.bat" || exit /b 1

echo Возвращаю твои приватные каналы и чаты в channels\private.json (этот файл в git не попадает).
echo.
python -m bottom tg restore-private %*
pause
