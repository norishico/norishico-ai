@echo off
REM ======================================================================
REM  Saturday preview (Task Scheduler: Fri 19:00)
REM  Saturday race prediction. Frames announced Fri 11:00, import training first.
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
set LOGFILE=%LOGDIR%\saturday_preview_%STAMP%.log

cd /d "%PROJ%"
echo [%date% %time%] saturday preview start >> "%LOGFILE%"

REM Step 1: training data import (TFJV DAT -> training)
echo [%date% %time%] training import >> "%LOGFILE%"
"%PYEXE%" -X utf8 scripts\import_training_from_tfjv.py >> "%LOGFILE%" 2>&1

REM Step 2: saturday prediction
echo [%date% %time%] publish_weekend --saturday >> "%LOGFILE%"
"%PYEXE%" -X utf8 publish_weekend.py --saturday >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] publish_weekend rc=%RC% >> "%LOGFILE%"

REM ???????????(2026-04-18 ????)
REM ?????????(morning_summary / added / cancelled / buy_go / daily_result)????

REM Dashboard ???(???????????????? 2026-04-19)
echo [%date% %time%] build_dashboard start >> "%LOGFILE%"
"%PYEXE%" -X utf8 build_dashboard.py >> "%LOGFILE%" 2>&1
echo [%date% %time%] build_dashboard rc=%ERRORLEVEL% >> "%LOGFILE%"


REM Step 3: Sanrenpuku jiku notification
echo [%date% %time%] sanrenpuku jiku notify >> "%LOGFILE%"
"%PYEXE%" -X utf8 notify_sanrenpuku_weekly.py --day sat >> "%LOGFILE%" 2>&1
echo [%date% %time%] sanrenpuku rc=%ERRORLEVEL% >> "%LOGFILE%"
endlocal & exit /b %RC%
