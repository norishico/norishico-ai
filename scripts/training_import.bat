@echo off
REM ======================================================================
REM  TFJV調教データ自動インポート (Task Scheduler: 毎晩 20:00)
REM  C:\TFJV\CK_DATA\*.DAT → keiba.db training テーブル
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
set LOGFILE=%LOGDIR%\training_import_%STAMP%.log

cd /d "%PROJ%"
echo [%date% %time%] TFJV training import start >> "%LOGFILE%"
"%PYEXE%" -X utf8 scripts\import_training_from_tfjv.py >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] rc=%RC% >> "%LOGFILE%"

endlocal & exit /b %RC%
