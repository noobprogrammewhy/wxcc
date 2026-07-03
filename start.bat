@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONPATH=%~dp0
echo Starting wxcc bridge... wait for "bridge up", then message the bot on WeChat.
echo Close this window to stop.
echo.
".venv\Scripts\python.exe" -u -m wxcc.cli run
echo.
echo Process exited. Press any key to close.
pause >nul
