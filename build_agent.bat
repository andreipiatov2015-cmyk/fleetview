@echo off
cd /d "%~dp0"
title Сборка агента FleetView
REM Запускать на МАСТЕР-компьютере, где установлен Python.
REM Результат: папка agent_package с FleetViewAgent.exe и install_agent.bat.
REM Её копируют на компьютеры пользователей, Python там не нужен.

python -c "import sys; sys.exit(0)" >nul 2>&1
if errorlevel 1 goto :nopython

echo.
echo  [1/3] Устанавливаю PyInstaller...
python -m pip install --upgrade pyinstaller
if errorlevel 1 goto :err

echo.
echo  [2/3] Собираю FleetViewAgent.exe, это займёт около минуты...
python -m PyInstaller --noconfirm --clean --onefile --console --name FleetViewAgent agent.py
if errorlevel 1 goto :err

echo.
echo  [3/3] Готовлю папку agent_package...
if not exist "%~dp0agent_package" mkdir "%~dp0agent_package"
copy /y "%~dp0dist\FleetViewAgent.exe" "%~dp0agent_package\" >nul
copy /y "%~dp0install_agent.bat" "%~dp0agent_package\" >nul

echo.
echo  ГОТОВО. Скопируйте папку agent_package на каждый компьютер пользователя,
echo  например в C:\FleetView, и запустите там install_agent.bat
echo  от имени администратора.
explorer "%~dp0agent_package"
goto :done

:nopython
echo.
echo  ОШИБКА: Python не найден. Сборку нужно запускать на мастер-компьютере,
echo  где работает консоль FleetView.
goto :done

:err
echo.
echo  ОШИБКА сборки, подробности в сообщениях выше.

:done
echo.
pause
