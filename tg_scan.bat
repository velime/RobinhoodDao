@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
call ".venv\Scripts\activate.bat"
echo Проверяю каналы из channels\all.txt - это займёт несколько минут...
python -m swarm.tg scan %*
if exist "channels\report.md" notepad "channels\report.md"
pause
