@echo off
REM ======================================================================
REM  Race-day auto_refresh starter (Task Scheduler: daily 09:00)
REM  - Launch if today's weekend_predictions.json has races
REM  - Exit immediately if none (weekdays do nothing)
REM  - Sun adds --sunday, Mon adds --monday
REM  - 09:00 start: first --once forced check (morning snapshot+publish)
REM    then enter auto_refresh.py normal loop (trigger 10min before post)
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

REM Day-of-week via PowerShell (0=Sun 1=Mon ... 6=Sat)
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek.value__"') do set DOW=%%i
echo [%date% %time%] DayOfWeek=%DOW% >> "%LOGFILE%"

REM Exit if not Sat/Sun/Mon (Sat=6, Sun=0, Mon=1)
if not "%DOW%"=="0" if not "%DOW%"=="1" if not "%DOW%"=="6" (
  echo [%date% %time%] non-race day, exit >> "%LOGFILE%"
  exit /b 0
)

REM Check if any race exists for today
"%PYEXE%" -X utf8 -c "import json,datetime; d=json.load(open('weekend_predictions.json',encoding='utf-8')); today=datetime.date.today().strftime('%%Y-%%m-%%d'); n=sum(1 for p in d if p.get('race',{}).get('race_id','').startswith(today.replace('-','')[:4]+'_')); races=[p for p in d if p.get('race',{}).get('start_time')]; import sys; sys.exit(0 if races else 2)" >> "%LOGFILE%" 2>&1
set HAS_RACE=%ERRORLEVEL%
if "%HAS_RACE%"=="2" (
  echo [%date% %time%] no races today, exit >> "%LOGFILE%"
  exit /b 0
)

REM Day-specific flag
set DAYFLAG=
if "%DOW%"=="0" set DAYFLAG=--sunday
if "%DOW%"=="1" set DAYFLAG=--monday

REM Step 1: 09:00 initial forced check (once only: refetch odds + buy decision + git push)
echo [%date% %time%] initial once-check start flag=%DAYFLAG% >> "%LOGFILE%"
"%PYEXE%" -X utf8 auto_refresh.py %DAYFLAG% --once >> "%LOGFILE%" 2>&1
echo [%date% %time%] initial once-check done rc=%ERRORLEVEL% >> "%LOGFILE%"

REM Step 2: normal loop (monitor each race 10min-before-post trigger)
echo [%date% %time%] auto_refresh loop start flag=%DAYFLAG% >> "%LOGFILE%"
"%PYEXE%" -X utf8 auto_refresh.py %DAYFLAG% >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] auto_refresh rc=%RC% >> "%LOGFILE%"

endlocal & exit /b %RC%
