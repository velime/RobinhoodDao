@echo off
chcp 65001 >nul
call "%~dp0_env.bat" || exit /b 1

echo === Самообучение Bottom: разовый прогон по всей истории ===
echo Выгрузки референса берутся из папки references\ (или перетащи файл result.json на этот .bat).
echo Уроки пишет Gemini через Antigravity CLI (agy); запасная - API-модель из .env.
echo Займёт от десятков минут: свечи по каждой монете и несколько запусков agy.
echo.
if not defined AGY_EFFORT set AGY_EFFORT=medium
if not defined AGY_TIMEOUT_MIN set AGY_TIMEOUT_MIN=10
python -m bottom selftrain --agy %*
pause
