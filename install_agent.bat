@echo off
cd /d "%~dp0"
title Установка агента FleetView

REM ---- 1. Права администратора ----
net session >nul 2>&1
if errorlevel 1 goto :noadmin

REM ---- 2. Готовый FleetViewAgent.exe (Python не нужен) ----
if exist "%~dp0FleetViewAgent.exe" goto :runexe

REM ---- 3. Запасной вариант: скрипт + установленный Python ----
if not exist "%~dp0agent.py" goto :nofiles
python -c "import sys; sys.exit(0)" >nul 2>&1
if errorlevel 1 goto :nopython
python "%~dp0agent.py" --configure
goto :done

:runexe
"%~dp0FleetViewAgent.exe" --configure
goto :done

:noadmin
echo.
echo  ОШИБКА: нужны права администратора.
echo  Щёлкните по install_agent.bat ПРАВОЙ кнопкой мыши
echo  и выберите "Запуск от имени администратора".
goto :done

:nofiles
echo.
echo  ОШИБКА: рядом с этим файлом нет FleetViewAgent.exe.
echo  Возможно, архив не распакован. Сначала распакуйте его в папку,
echo  например C:\FleetView, и запустите install_agent.bat оттуда.
goto :done

:nopython
echo.
echo  ОШИБКА: на этом компьютере не установлен Python,
echo  а рядом нет готового FleetViewAgent.exe.
echo  Решение: на МАСТЕР-компьютере запустите build_agent.bat,
echo  затем скопируйте сюда папку agent_package и запустите
echo  install_agent.bat из неё.
goto :done

:done
echo.
pause
