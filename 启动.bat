@echo off
rem Launch the UI window (UI.pyc if compiled, otherwise UI.py).
cd /d "%~dp0"
set LAUNCH=UI.py
if exist "UI.pyc" set LAUNCH=UI.pyc
if exist "deps\python\pythonw.exe" (
    start "" "deps\python\pythonw.exe" %LAUNCH%
) else (
    start "" pythonw %LAUNCH%
)
exit /b 0
