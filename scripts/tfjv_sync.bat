@echo off
REM ======================================================================
REM  TFJV 自動同期 (Task Scheduler 用ラッパ)
REM  実行: 毎朝 06:30 (調教データが夜中更新される想定)
REM ======================================================================
setlocal
set PROJ=C:\Users\westr\norishiko_ai

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJ%\scripts\tfjv_sync.ps1"
set RC=%ERRORLEVEL%
exit /b %RC%
