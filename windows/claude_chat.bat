@echo off
chcp 65001 >nul
call "%~dp0_env.bat" || exit /b 1

set "PATH=%USERPROFILE%\.local\bin;%PATH%"
python -m bottom claude setup >nul
where claude >nul 2>nul
if errorlevel 1 (
  echo [X] Claude Code не найден. Сначала запусти claude_setup.bat
  pause
  exit /b 1
)
rem Фоновый сборщик Telegram наполняет базу, пока открыт чат (если он уже запущен - второй сам закроется).
start "Bottom collector" /min cmd /c "python -m bottom tg collect"
cd claude_workspace
echo Bottom в Claude Code: спрашивай как в Telegram. Все инструменты подключены (биржи, стаканы, Alpha, каналы, X, интернет).
echo.
claude
pause
