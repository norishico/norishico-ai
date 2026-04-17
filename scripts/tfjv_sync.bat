@echo off
REM ======================================================================
REM  TFJV auto sync wrapper (Task Scheduler)
REM  Runs daily 06:30 (training data updated overnight)
REM ======================================================================
setlocal
set PROJ=C:\Users\westr\norishiko_ai

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJ%\scripts\tfjv_sync.ps1"
set RC=%ERRORLEVEL%
exit /b %RC%
