@echo off
setlocal
cd /d "%~dp0"
where uv >nul 2>nul
if not errorlevel 1 (
    if not exist ".venv\Scripts\python.exe" uv venv --python 3.12 --no-python-downloads .venv
    if errorlevel 1 goto fail
    uv pip install --python ".venv\Scripts\python.exe" -r requirements.txt
    if errorlevel 1 goto fail
    goto done
)
if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv
    if errorlevel 1 python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" goto fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
:done
echo Ready. Double-click start.bat.
pause
exit /b 0
:fail
echo Setup failed. Check that Python 3.12 x64 with Tcl/Tk is installed.
echo See README.md for manual setup.
pause
exit /b 1
