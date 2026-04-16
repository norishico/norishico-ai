@echo off
REM ======================================================================
REM  Saturday preview (Task Scheduler: 金曜19:00)
REM  土曜レース予想。枠順は金曜11時発表済、最新調教取り込み後スコアリング
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

REM Step 1: 最新調教データ取り込み (TFJV DAT → training)
echo [%date% %time%] training import >> "%LOGFILE%"
"%PYEXE%" -X utf8 scripts\import_training_from_tfjv.py >> "%LOGFILE%" 2>&1

REM Step 2: 土曜レース予想
echo [%date% %time%] publish_weekend --saturday >> "%LOGFILE%"
"%PYEXE%" -X utf8 publish_weekend.py --saturday >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] publish_weekend rc=%RC% >> "%LOGFILE%"

REM Discord通知: 予想公開
if "%RC%"=="0" (
  "%PYEXE%" -X utf8 -c "import json; from scripts.notify import notify_prediction_ready; preds=json.load(open('weekend_predictions.json',encoding='utf-8')); notify_prediction_ready(preds,'土曜予想')" >> "%LOGFILE%" 2>&1
)

endlocal & exit /b %RC%
