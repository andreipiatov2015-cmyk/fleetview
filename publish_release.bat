@echo off
cd /d "%~dp0"
chcp 65001 >nul
title Публикация релиза FleetView
REM Запускать на мастере после того, как вы подняли номер в version.py.
REM Делает коммит, ставит тег vX.Y.Z и пушит - дальше GitHub Actions сам
REM соберёт exe и опубликует релиз.

where git >nul 2>&1
if errorlevel 1 (
  echo ОШИБКА: не установлен Git. Скачайте с https://git-scm.com
  pause & exit /b 1
)

for /f "delims=" %%v in ('python -c "import version;print(version.VERSION)"') do set VER=%%v
if "%VER%"=="" (
  echo ОШИБКА: не удалось прочитать версию из version.py
  pause & exit /b 1
)

echo Готовлю релиз версии %VER%
git add -A
git commit -m "Release v%VER%"
git tag v%VER%
git push origin HEAD
git push origin v%VER%
if errorlevel 1 (
  echo.
  echo Пуш не прошёл. Проверьте, что репозиторий привязан:
  echo   git remote add origin https://github.com/andreipiatov2015-cmyk/fleetview.git
  pause & exit /b 1
)

echo.
echo Готово. GitHub Actions соберёт exe и опубликует релиз v%VER%.
echo Следите за ходом: https://github.com/andreipiatov2015-cmyk/fleetview/actions
pause
