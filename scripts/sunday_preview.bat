@echo off
REM ======================================================================
REM  Sunday preview (Task Scheduler: Sat 20:00)
REM  Sunday race prediction. Runs after Saturday races end to avoid
REM  conflict with race_day_auto_refresh loop (2026-04-18 policy change).
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
set LOGFILE=%LOGDIR%\sunday_preview_%STAMP%.log

cd /d "%PROJ%"
echo [%date% %time%] sunday preview start >> "%LOGFILE%"

REM Step 1: training data import (TFJV DAT -> training)
echo [%date% %time%] training import >> "%LOGFILE%"
"%PYEXE%" -X utf8 scripts\import_training_from_tfjv.py >> "%LOGFILE%" 2>&1

REM Step 2: sunday prediction
REM NOTE: --sunday ????????? (????????? 2026-04-18 ??)
REM ????????????????????
echo [%date% %time%] publish_weekend (both sat+sun) >> "%LOGFILE%"
"%PYEXE%" -X utf8 publish_weekend.py >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] rc=%RC% >> "%LOGFILE%"

REM ???????????(2026-04-18 ????)
REM ?????????(morning_summary / added / cancelled / buy_go / daily_result)????

REM Dashboard ???(???????????????? 2026-04-19)
echo [%date% %time%] build_dashboard start >> "%LOGFILE%"
"%PYEXE%" -X utf8 build_dashboard.py >> "%LOGFILE%" 2>&1
echo [%date% %time%] build_dashboard rc=%ERRORLEVEL% >> "%LOGFILE%"

REM Step 3: Sanrenpuku jiku notification (Sunday)
echo [%date% %time%] sanrenpuku jiku notify (sun) >> "%LOGFILE%"
"%PYEXE%" -X utf8 notify_sanrenpuku_weekly.py --day sun >> "%LOGFILE%" 2>&1
echo [%date% %time%] sanrenpuku rc=%ERRORLEVEL% >> "%LOGFILE%"
endlocal & exit /b %RC%
