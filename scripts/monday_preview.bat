@echo off
REM ======================================================================
REM  Monday preview (Task Scheduler: Sun 11:00)
REM  Holiday monday support. Frames announced Sun 09:00.
REM  Empty on non-monday weeks (no side effect).
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
set LOGFILE=%LOGDIR%\monday_preview_%STAMP%.log

cd /d "%PROJ%"
echo [%date% %time%] monday preview start >> "%LOGFILE%"

REM Get tomorrow (monday) date as YYYYMMDD
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).AddDays(1).ToString('yyyyMMdd')"') do set MONDAY=%%i
echo [%date% %time%] target date=%MONDAY% >> "%LOGFILE%"

REM Step 1: training data import
"%PYEXE%" -X utf8 scripts\import_training_from_tfjv.py >> "%LOGFILE%" 2>&1

REM Step 2: monday prediction (--date specifies target day)
"%PYEXE%" -X utf8 publish_weekend.py --date %MONDAY% >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] monday preview rc=%RC% >> "%LOGFILE%"

endlocal & exit /b %RC%
