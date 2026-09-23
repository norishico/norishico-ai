@echo off
REM ======================================================================
REM  MC123 tier refresh (Task Scheduler): weekly, read-only DB access
REM  analyze_mc123_top1_conditions.py -> compute_mc123_top1_reliability.py
REM  -> compute_formation_accuracy.py -> build_course_tiers_json.py -> deploy
REM  Refreshes mc123_top1_conditions.json / mc123_top1_reliability.json /
REM  formation_accuracy.json, consumed by generate_mc123_forecast.py and
REM  generate_pace_forecast.py on the next scheduled MCKeibaGenerate run.
REM  2026-09-04: created after discovering these 3 scripts had no scheduled
REM  run since being written in August (design gap, not a bug)
REM  2026-09-23 (S6): added build_course_tiers_json.py (mc_keiba_public/
REM  course_tiers.json for the AYOkeiba "course list" tab) + a Vercel prod
REM  deploy at the end of this weekly run (のりお承認済み: this is now the
REM  only path that ships course_tiers.json, since the daily MCKeibaGenerate
REM  deploy does not regenerate it)
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
set LOGFILE=%LOGDIR%\mc123_tier_refresh_%STAMP%.log

cd /d "%PROJ%"
echo [%date% %time%] mc123 tier refresh start >> "%LOGFILE%"

"%PYEXE%" -X utf8 analyze_mc123_top1_conditions.py >> "%LOGFILE%" 2>&1
echo [%date% %time%] analyze_mc123_top1_conditions rc=%ERRORLEVEL% >> "%LOGFILE%"

"%PYEXE%" -X utf8 compute_mc123_top1_reliability.py >> "%LOGFILE%" 2>&1
echo [%date% %time%] compute_mc123_top1_reliability rc=%ERRORLEVEL% >> "%LOGFILE%"

"%PYEXE%" -X utf8 compute_formation_accuracy.py >> "%LOGFILE%" 2>&1
echo [%date% %time%] compute_formation_accuracy rc=%ERRORLEVEL% >> "%LOGFILE%"

"%PYEXE%" -X utf8 build_course_tiers_json.py >> "%LOGFILE%" 2>&1
echo [%date% %time%] build_course_tiers_json rc=%ERRORLEVEL% >> "%LOGFILE%"

echo [%date% %time%] Vercel deploy start >> "%LOGFILE%"
vercel --cwd "%PROJ%\mc_keiba_public" --prod --yes >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] Vercel deploy rc=%RC% >> "%LOGFILE%"

endlocal & exit /b %RC%
