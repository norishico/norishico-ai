@echo off
REM ======================================================================
REM  Sunday preview (Task Scheduler: Sat 11:00)
REM  Sunday race prediction. Frames announced Sat 09:00, import training first.
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
echo [%date% %time%] publish_weekend --sunday >> "%LOGFILE%"
"%PYEXE%" -X utf8 publish_weekend.py --sunday >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] rc=%RC% >> "%LOGFILE%"

REM Discord notify: prediction ready
if "%RC%"=="0" (
  "%PYEXE%" -X utf8 -c "import json; from scripts.notify import notify_prediction_ready; preds=json.load(open('weekend_predictions.json',encoding='utf-8')); notify_prediction_ready(preds,'sun')" >> "%LOGFILE%" 2>&1
)

endlocal & exit /b %RC%
