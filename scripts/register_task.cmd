@echo off
REM Registers a Windows Task Scheduler job that runs Award-to-Lane every weekday at 06:30.
REM Run this once from a normal command prompt (no admin rights needed for a per-user task).
set BAT=%~dp0run_daily.bat
schtasks /Create /F /SC WEEKLY /D MON,TUE,WED,THU,FRI /TN "Award-to-Lane daily" /TR "\"%BAT%\"" /ST 06:30
if %ERRORLEVEL%==0 echo Registered. Check Task Scheduler Library for "Award-to-Lane daily". Run now with: schtasks /Run /TN "Award-to-Lane daily"
