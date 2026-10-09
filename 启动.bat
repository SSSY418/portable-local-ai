@echo off
chcp 65001 >nul
setlocal
title Portable AI Workbench

rem Locate the project root with %~dp0 -- never hardcode a drive letter.
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

rem NOTE: this file is deliberately pure ASCII. cmd.exe parses .bat files using the
rem system ANSI code page, so UTF-8 Chinese text here can corrupt the parse (variables
rem silently become empty). All Chinese wording lives in _tools\start.ps1 instead.
rem
rem Cleanup note: when you close this window, workbench.py (which runs in the
rem foreground below) receives Windows' console-close notification and stops
rem koboldcpp itself. That is more reliable than trying to do it here, because
rem Windows only gives a closing cmd.exe a couple of seconds to finish.
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\_tools\start.ps1"
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
  echo.
  echo Workbench stopped with code %CODE%. See the messages above.
  echo Press any key to close this window...
  pause >nul
)
endlocal
exit /b %CODE%