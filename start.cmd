@echo off
setlocal
pushd "%~dp0" || exit /b 1
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
set "PYTHON_ARGS="
rem Opens the interface. "start run ..." (sts-agent start-run) opens it too and begins a new run in it;
rem "start run --console ..." plays without the interface.
set "AGENT_COMMAND=ui"
if /i "%~1"=="run" (
    set "AGENT_COMMAND=start-run"
    shift
)
set "AGENT_ARGS="
:collect
if "%~1"=="" goto collected
set "AGENT_ARGS=%AGENT_ARGS% %1"
shift
goto collect
:collected

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
"%PYTHON%" %PYTHON_ARGS% -u -m sts_agent %AGENT_COMMAND%%AGENT_ARGS%
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
