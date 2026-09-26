@echo off
REM -- 2026-07-23 fix: this task runs at 17:30 same-day, but fetch_saturday_results.py
REM    defaults to "yesterday" when --date is not given, so it used to always check
REM    the wrong (previous, often race-free Friday) day. Pass today's date explicitly.
REM -- 2026-09-27: rewrote with plain-ASCII comments only and Python-based date
REM    computation (see mc_keiba_generate.bat for the full explanation).
echo [marker] batch triggered %date% %time% >> "C:\Users\westr\norishiko_ai\logs\saturday_results_lastrun_marker.log" 2>&1
setlocal
set ROOT=C:\Users\westr\norishiko_ai
set LOGDIR=%ROOT%\logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

for /f "usebackq" %%d in (`py -c "import datetime; print(datetime.date.today().strftime('%%Y%%m%%d'))"`) do set TODAY=%%d

py -X utf8 "%ROOT%\fetch_saturday_results.py" --date %TODAY% >> "%LOGDIR%\saturday_results.log" 2>&1

REM -- fetch Saturday payout data from the web --
call "%ROOT%\scripts\dividends_web.bat"
