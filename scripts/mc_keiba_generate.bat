@echo off
REM mc_keiba_generate.bat - Task Scheduler: SAT+SUN 05:00
REM generate_mc_record.py (widget) + generate_pace_forecast.py (pace sim) +
REM generate_mc123_forecast.py (MC123) -> vercel deploy
REM 2026-08-10: generate_mc_record.py now only builds widget_data.json.
REM 2026-08-13: pace_data.json / mc123_data.json generation folded into this batch.
REM These two are independent (a failure just leaves stale data, no impact on the
REM main widget), so their own failures do not block the deploy step; only
REM generate_mc_record.py acts as the canary that blocks the deploy.
REM 2026-08-23: added win5_data.json generation (previously manual-only, would go stale).
REM 2026-09-20 F5: accumulate per-step failures into FAIL so the final exit code
REM always reflects a failure even though the deploy step still runs regardless.
REM 2026-09-27: rewrote this file with plain-ASCII comments only. The previous
REM version had Japanese REM comments saved as UTF-8, and on this Japanese-locale
REM Windows box cmd.exe parses batch files using the ANSI codepage (CP932/Shift-JIS)
REM by default, which corrupted the REM keyword itself in several lines and made
REM cmd.exe try to execute the garbled text as a command. This caused the task to
REM fail immediately with zero log output, both under the original interactive
REM logon and after switching to S4U. Same lesson as feedback_ps1_writing.md
REM (avoid non-ASCII text in files cmd.exe/PowerShell must parse). Also replaced
REM the locale-dependent %date%/%time% parsing and the nested "powershell -command"
REM call (both plausible contributors) with plain Python date computation, and
REM added a marker log written before any other step so a future failure this
REM early still leaves a trace.
echo [marker] batch triggered %date% %time% >> "C:\Users\westr\norishiko_ai\logs\mc_keiba_generate_lastrun_marker.log" 2>&1
setlocal
set PROJ=C:\Users\westr\norishiko_ai
set PYEXE=py
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set LOGDIR=%PROJ%\logs

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

for /f "usebackq" %%i in (`%PYEXE% -c "import datetime; print(datetime.datetime.now().strftime('%%Y%%m%%d_%%H%%M%%S'))"`) do set STAMP=%%i
set LOGFILE=%LOGDIR%\mc_keiba_generate_%STAMP%.log

for /f "usebackq" %%i in (`%PYEXE% -c "import datetime; print(datetime.date.today().isoformat())"`) do set TODAY=%%i

set FAIL=0

cd /d "%PROJ%"
echo [%date% %time%] MC Keiba generate start TODAY=%TODAY% >> "%LOGFILE%"
"%PYEXE%" -X utf8 generate_mc_record.py %TODAY% >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] generate rc=%RC% >> "%LOGFILE%"

if %RC% NEQ 0 (
  set FAIL=1
  goto :end
)

echo [%date% %time%] pace forecast start >> "%LOGFILE%"
"%PYEXE%" -X utf8 generate_pace_forecast.py %TODAY% >> "%LOGFILE%" 2>&1
set RC_PACE=%ERRORLEVEL%
echo [%date% %time%] pace forecast rc=%RC_PACE% >> "%LOGFILE%"
if %RC_PACE% NEQ 0 set FAIL=1

echo [%date% %time%] mc123 forecast start >> "%LOGFILE%"
"%PYEXE%" -X utf8 generate_mc123_forecast.py %TODAY% >> "%LOGFILE%" 2>&1
set RC_MC123=%ERRORLEVEL%
echo [%date% %time%] mc123 forecast rc=%RC_MC123% >> "%LOGFILE%"
if %RC_MC123% NEQ 0 set FAIL=1

echo [%date% %time%] win5 data start >> "%LOGFILE%"
"%PYEXE%" -X utf8 generate_win5_data.py >> "%LOGFILE%" 2>&1
set RC_WIN5=%ERRORLEVEL%
echo [%date% %time%] win5 data rc=%RC_WIN5% >> "%LOGFILE%"
if %RC_WIN5% NEQ 0 set FAIL=1

echo [%date% %time%] Vercel deploy start >> "%LOGFILE%"
vercel --cwd "%PROJ%\mc_keiba_public" --prod --yes >> "%LOGFILE%" 2>&1
set RC2=%ERRORLEVEL%
echo [%date% %time%] Vercel deploy rc=%RC2% >> "%LOGFILE%"
if %RC2% NEQ 0 set FAIL=1

:end
echo [%date% %time%] MC Keiba generate done FAIL=%FAIL% >> "%LOGFILE%"
endlocal & exit /b %FAIL%
