@echo off
REM ======================================================================
REM  Race-day auto_refresh starter (Task Scheduler: 毎日 09:00)
REM  - 今日の weekend_predictions.json にレースがあれば起動
REM  - 無ければ即終了 (平日は何もしない)
REM  - 日曜は --sunday、月曜は --monday フラグ付与
REM  - 09:00 起動: まず --once で強制1回チェック (朝の最新スナップショット+公開)
REM    その後 auto_refresh.py 通常ループで発走10分前トリガー監視に入る
REM ======================================================================
setlocal EnableDelayedExpansion
set PROJ=C:\Users\westr\norishiko_ai
set PYEXE=py
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set LOGDIR=%PROJ%\logs

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

set STAMP=%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%
set STAMP=%STAMP: =0%
set LOGFILE=%LOGDIR%\race_day_auto_refresh_%STAMP%.log

cd /d "%PROJ%"
echo [%date% %time%] race-day auto_refresh check >> "%LOGFILE%"

REM 曜日取得 (PowerShell経由、0=Sun 1=Mon ... 6=Sat)
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set DOW=%%i
echo [%date% %time%] DayOfWeek=%DOW% >> "%LOGFILE%"

REM 土日月以外は終了 (Sat=6, Sun=0, Mon=1)
if not "%DOW%"=="0" if not "%DOW%"=="1" if not "%DOW%"=="6" (
  echo [%date% %time%] non-race day, exit >> "%LOGFILE%"
  exit /b 0
)

REM 今日のレースがあるか確認
"%PYEXE%" -X utf8 -c "import json,datetime; d=json.load(open('weekend_predictions.json',encoding='utf-8')); today=datetime.date.today().strftime('%%Y-%%m-%%d'); n=sum(1 for p in d if p.get('race',{}).get('race_id','').startswith(today.replace('-','')[:4]+'_')); races=[p for p in d if p.get('race',{}).get('start_time')]; import sys; sys.exit(0 if races else 2)" >> "%LOGFILE%" 2>&1
set HAS_RACE=%ERRORLEVEL%
if "%HAS_RACE%"=="2" (
  echo [%date% %time%] no races today, exit >> "%LOGFILE%"
  exit /b 0
)

REM 曜日別フラグ
set DAYFLAG=
if "%DOW%"=="0" set DAYFLAG=--sunday
if "%DOW%"=="1" set DAYFLAG=--monday

REM Step 1: 09:00 初回強制チェック (1回だけ、オッズ再取得+買い判定+git push)
echo [%date% %time%] initial once-check start flag=%DAYFLAG% >> "%LOGFILE%"
"%PYEXE%" -X utf8 auto_refresh.py %DAYFLAG% --once >> "%LOGFILE%" 2>&1
echo [%date% %time%] initial once-check done rc=%ERRORLEVEL% >> "%LOGFILE%"

REM Step 2: 通常ループ起動 (各レース発走10分前トリガー監視)
echo [%date% %time%] auto_refresh loop start flag=%DAYFLAG% >> "%LOGFILE%"
"%PYEXE%" -X utf8 auto_refresh.py %DAYFLAG% >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] auto_refresh rc=%RC% >> "%LOGFILE%"

endlocal & exit /b %RC%
