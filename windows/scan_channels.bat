@echo off
chcp 65001 >nul
call "%~dp0_env.bat" || exit /b 1

python -m bottom tg scan %*
if exist "channels\report.md" notepad "channels\report.md"
pause
