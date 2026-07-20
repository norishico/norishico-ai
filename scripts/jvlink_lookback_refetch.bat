@echo off
REM ======================================================================
REM  JV-Link monthly lookback re-fetch (Windows Task Scheduler)
REM  Re-pulls the past N days to recover late-confirmed fields
REM  (odds/prize_won etc.) that narrow diff fetches never catch up on.
REM  Runs the normal staging -> count-check -> atomic swap pipeline.
REM ======================================================================
setlocal
set PROJ=C:\Users\westr\norishiko_ai
set PYEXE=py
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set LOGDIR=%PROJ%\logs

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

set STAMP=%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%
set STAMP=%STAMP: =0%
set LOGFILE=%LOGDIR%\lookback_refetch_%STAMP%.log

cd /d "%PROJ%"
echo [%date% %time%] JV-Link lookback refetch start >> "%LOGFILE%"
"%PYEXE%" fetch_and_build.py --lookback-days 90 >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] rc=%RC% >> "%LOGFILE%"

endlocal & exit /b %RC%
