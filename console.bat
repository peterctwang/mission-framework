@echo off
REM Double-click to open the interactive TUI console for the current dir.
REM Pass a path to point at another project: console.bat C:\path\to\project
setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if "%~1"=="" (
    python -m harness.cli console
) else (
    python -m harness.cli console "%~1"
)
endlocal
