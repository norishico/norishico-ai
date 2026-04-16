@echo off
REM ======================================================================
REM  JV-Link Phase 2 parallel fetch (Windows Task Scheduler)
REM  Updates keiba_staging.db via --parallel mode (no swap to prod)
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
set LOGFILE=%LOGDIR%\parallel_fetch_%STAMP%.log

cd /d "%PROJ%"
echo [%date% %time%] JV-Link parallel fetch start >> "%LOGFILE%"
"%PYEXE%" fetch_and_build.py --parallel >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] rc=%RC% >> "%LOGFILE%"

REM Run diff check after successful fetch
if "%RC%"=="0" (
  if exist "%PROJ%\scripts\diff_sources.py" (
    "%PYEXE%" "%PROJ%\scripts\diff_sources.py" >> "%LOGFILE%" 2>&1
  )
)

endlocal & exit /b %RC%
