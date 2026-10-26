@echo off
chcp 65001 >nul
call "%~dp0_env.bat" || exit /b 1

python -m bottom check
pause
