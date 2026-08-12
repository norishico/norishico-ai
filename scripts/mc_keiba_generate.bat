@echo off
REM mc_keiba_generate.bat - Task Scheduler: SAT+SUN 05:00
REM generate_mc_record.py(widget) + generate_pace_forecast.py(展開予想) +
REM generate_mc123_forecast.py(MC123) -> vercel deploy
REM 2026-08-10: 「記録履歴」タブ撤去に伴いgenerate_mc_record.pyの役割はウィジェット用
REM widget_data.json生成のみに縮小(index.htmlはもう書き換えない)。
REM 2026-08-13: pace_data.json/mc123_data.jsonの自動生成をbatに組み込み(既知の課題を解消)。
REM この2本は失敗しても独立設計(古いデータが残るだけで本体・ウィジェットには影響しない)
REM ため、個別の失敗ではデプロイ自体は止めない(generate_mc_record.pyの失敗のみデプロイを
REM 止めるcanaryとして扱う)
setlocal
set PROJ=C:\Users\westr\norishiko_ai
set PYEXE=py
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set LOGDIR=%PROJ%\logs

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

set STAMP=%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%
set STAMP=%STAMP: =0%
set LOGFILE=%LOGDIR%\mc_keiba_generate_%STAMP%.log

for /f %%i in ('powershell -command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i

cd /d "%PROJ%"
echo [%date% %time%] MC Keiba generate start TODAY=%TODAY% >> "%LOGFILE%"
"%PYEXE%" -X utf8 generate_mc_record.py %TODAY% >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] generate rc=%RC% >> "%LOGFILE%"

if %RC% NEQ 0 goto :end

echo [%date% %time%] pace forecast start >> "%LOGFILE%"
"%PYEXE%" -X utf8 generate_pace_forecast.py %TODAY% >> "%LOGFILE%" 2>&1
echo [%date% %time%] pace forecast rc=%ERRORLEVEL% >> "%LOGFILE%"

echo [%date% %time%] mc123 forecast start >> "%LOGFILE%"
"%PYEXE%" -X utf8 generate_mc123_forecast.py %TODAY% >> "%LOGFILE%" 2>&1
echo [%date% %time%] mc123 forecast rc=%ERRORLEVEL% >> "%LOGFILE%"

echo [%date% %time%] Vercel deploy start >> "%LOGFILE%"
vercel --cwd "%PROJ%\mc_keiba_public" --prod --yes >> "%LOGFILE%" 2>&1
set RC2=%ERRORLEVEL%
echo [%date% %time%] Vercel deploy rc=%RC2% >> "%LOGFILE%"

:end
endlocal & exit /b %RC%