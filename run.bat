@echo off
REM Double-click to start the planner.
REM This window IS the server -- leave it open while you use the dashboard.

cd /d "%~dp0"
call .venv\Scripts\activate.bat
python scripts\fetch_brightspace.py
start "" http://127.0.0.1:8787
python scripts\serve.py
pause
