@echo off
setlocal
pushd "%~dp0" || exit /b 1
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
set "PYTHON_ARGS="

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
    goto run
)
if exist ".venv\python.exe" (
    set "PYTHON=.venv\python.exe"
    goto run
)
where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON=py"
    set "PYTHON_ARGS=-3"
    goto run
)
where python >nul 2>nul
if not errorlevel 1 (
    set "PYTHON=python"
    goto run
)
where python3 >nul 2>nul
if not errorlevel 1 (
    set "PYTHON=python3"
    goto run
)
echo Python 3.11 or newer is required. Install Python or create a .venv first. >&2
popd
exit /b 9009

:run
"%PYTHON%" %PYTHON_ARGS% -u -m sts_agent start-run %*
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
