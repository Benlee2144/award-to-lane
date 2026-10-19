@echo off
REM Award-to-Lane daily run (Windows). Called by Task Scheduler; safe to double-click.
cd /d "%~dp0\.."
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python run_daily.py %*
