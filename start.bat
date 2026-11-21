@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" app.py
    if not errorlevel 1 exit /b 0
    pause
    exit /b 1
)
echo First run: double-click setup.bat to install dependencies.
pause
exit /b 1
