@echo off
rem ============================================================
rem  QQ Agent Desktop - one-click launcher
rem  Keep this file ASCII-only for cmd compatibility.
rem  PLACEMENT: ME_Agent repo ROOT (the parent directory of desktop\).
rem  Do NOT move it into desktop\ - all paths below are relative
rem  to the repo root on purpose.
rem  Order of preference:
rem    1) Packaged build: desktop\dist\QQAgentDesktop.exe
rem       (also matched: desktop\dist\*-win32-x64\QQAgentDesktop.exe)
rem    2) Dev runtime:    desktop\node_modules\electron\dist\electron.exe
rem    3) Both missing:   repair hints + pause (exit 1).
rem  Success path: start "" /b, silent, no pause, no extra window.
rem ============================================================
setlocal EnableExtensions
set "DESKTOP=%~dp0desktop"

set "APP="
if exist "%DESKTOP%\dist\QQAgentDesktop.exe" set "APP=%DESKTOP%\dist\QQAgentDesktop.exe"
if not defined APP (
  for /d %%D in ("%DESKTOP%\dist\*") do (
    if not defined APP if exist "%%D\QQAgentDesktop.exe" set "APP=%%D\QQAgentDesktop.exe"
  )
)
set "ELECTRON=%DESKTOP%\node_modules\electron\dist\electron.exe"

rem Packaged build: self-contained, starts silently.
if defined APP (
  rem WorkBuddy/Electron hosts may leak this flag and force Electron into Node mode.
  set "ELECTRON_RUN_AS_NODE="
  start "" /b "%APP%"
  endlocal
  exit /b 0
)

rem Dev runtime: run local Electron with the desktop\ app dir.
if exist "%ELECTRON%" (
  set "ELECTRON_RUN_AS_NODE="
  pushd "%DESKTOP%"
  rem Use \., not a quoted path ending in a backslash: cmd otherwise passes a trailing quote to Electron.
  start "" /b "%ELECTRON%" "%DESKTOP%\."
  popd
  endlocal
  exit /b 0
)

echo QQ Agent Desktop runtime not found. Looked for:
echo   %DESKTOP%\dist\QQAgentDesktop.exe
echo   %DESKTOP%\dist\QQAgentDesktop-win32-x64\QQAgentDesktop.exe
echo   %ELECTRON%
echo.
echo Repair (pick one):
echo   1. cd desktop ^&^& npm install          installs the Electron dev runtime
echo   2. cd desktop ^&^& npm run package-win  builds desktop\dist\QQAgentDesktop.exe
echo Then double-click this file again.
pause
endlocal
exit /b 1
