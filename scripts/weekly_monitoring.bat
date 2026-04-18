@echo off
REM ======================================================================
REM  Weekly operational monitoring (Task Scheduler)
REM ======================================================================
setlocal
set PROJ=C:\Users\westr\norishiko_ai
set PYEXE=py
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set LOGDIR=%PROJ%\logs

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

set STAMP=%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%
set STAMP=%STAMP: =0%
set LOGFILE=%LOGDIR%\weekly_monitoring_%STAMP%.log

cd /d "%PROJ%"
echo [%date% %time%] weekly monitoring start >> "%LOGFILE%"
"%PYEXE%" scripts\run_monitoring.py >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] run_monitoring rc=%RC% >> "%LOGFILE%"

REM build dashboard.html from latest data
echo [%date% %time%] build_dashboard start >> "%LOGFILE%"
"%PYEXE%" -X utf8 build_dashboard.py >> "%LOGFILE%" 2>&1
echo [%date% %time%] build_dashboard rc=%ERRORLEVEL% >> "%LOGFILE%"

endlocal & exit /b %RC%
