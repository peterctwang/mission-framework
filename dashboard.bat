@echo off
REM Double-click for a one-shot rich snapshot. Window stays open so you can read it.
setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if "%~1"=="" (
    python -m harness.cli dashboard
) else (
    python -m harness.cli dashboard "%~1"
)
echo.
pause
endlocal
