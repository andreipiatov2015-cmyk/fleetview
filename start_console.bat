@echo off
cd /d "%~dp0"
REM Запуск мастер-консоли FleetView.
python -c "import PySide6" >nul 2>&1
if errorlevel 1 python -m pip install -r requirements.txt
start "" pythonw "%~dp0console.py"
