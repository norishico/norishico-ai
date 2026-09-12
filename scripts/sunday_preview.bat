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

REM Step 4: AYOkeiba (mc123/pace/widget/win5) early generation (added 2026-09-12)
REM Frames are already confirmed and publish_weekend.py above has just refetched
REM this_week_races.json / weekend_predictions.json for TOMORROW (Sunday's races),
REM so there is no need to wait for the raceday-morning MCKeibaGenerate task anymore.
REM generate_mc_record.py has its own staleness guard against weekend_predictions.json,
REM so if publish_weekend.py failed above, this step fails loud here too (goto :ayoend
REM skips pace/mc123/win5/deploy, same canary pattern as mc_keiba_generate.bat).
REM The 05:00 raceday MCKeibaGenerate task is kept as-is to catch overnight scratches.
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).AddDays(1).ToString('yyyy-MM-dd')"') do set TOMORROW=%%i
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).AddDays(1).ToString('yyyyMMdd')"') do set TOMORROW_C=%%i

echo [%date% %time%] AYOkeiba early generate start TOMORROW=%TOMORROW% >> "%LOGFILE%"
"%PYEXE%" -X utf8 generate_mc_record.py %TOMORROW% >> "%LOGFILE%" 2>&1
set RC3=%ERRORLEVEL%
echo [%date% %time%] generate_mc_record rc=%RC3% >> "%LOGFILE%"
if %RC3% NEQ 0 goto :ayoend

"%PYEXE%" -X utf8 generate_pace_forecast.py %TOMORROW% >> "%LOGFILE%" 2>&1
echo [%date% %time%] pace forecast rc=%ERRORLEVEL% >> "%LOGFILE%"

"%PYEXE%" -X utf8 generate_mc123_forecast.py %TOMORROW% >> "%LOGFILE%" 2>&1
echo [%date% %time%] mc123 forecast rc=%ERRORLEVEL% >> "%LOGFILE%"

"%PYEXE%" -X utf8 generate_win5_data.py %TOMORROW_C% >> "%LOGFILE%" 2>&1
echo [%date% %time%] win5 data rc=%ERRORLEVEL% >> "%LOGFILE%"

echo [%date% %time%] Vercel deploy start >> "%LOGFILE%"
vercel --cwd "%PROJ%\mc_keiba_public" --prod --yes >> "%LOGFILE%" 2>&1
echo [%date% %time%] Vercel deploy rc=%ERRORLEVEL% >> "%LOGFILE%"
:ayoend

endlocal & exit /b %RC%
