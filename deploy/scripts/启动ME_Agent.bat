@echo off
rem ============================================================
rem ME_Agent Windows launcher (console backend service).
rem Keep this file ASCII-only for cmd compatibility (GBK/437 safe).
rem Location: deploy\scripts\ ; repo root = two levels up.
rem Usage:
rem   double-click                  -> start on default port 8000
rem   this .bat --port 8080         -> extra args are passed to python -m app
rem Prerequisite: .venv created and requirements installed.
rem ============================================================
setlocal
cd /d "%~dp0..\.."

rem Force UTF-8 mode for the child Python (fixes GBK locale issues on
rem Chinese Windows: pip auto_decode / config files / state persistence).
rem pip reads requirements.txt with locale codec when no BOM present.
set "PYTHONUTF8=1"

set "VENV_PY=.venv\Scripts\python.exe"
set "REQ=requirements.txt"

rem ---- [1/2] venv existence check -----------------------------------
if not exist "%VENV_PY%" (
  echo [ME_Agent] ERROR: virtual env not found: %CD%\.venv
  echo.
  echo Fix guidance ^(run in this folder^):
  echo   python -m venv .venv
  echo   .venv\Scripts\pip.exe install -r %REQ%
  echo.
  echo Then run this script again.
  pause
  exit /b 1
)

rem ---- [2/2] key dependency import check ----------------------------
rem NOTE: python_multipart is the import name of the pip package
rem "python-multipart" (required by FastAPI form/file upload routes).
rem NOTE: tzdata is the IANA tz database for the zoneinfo module.
rem Windows has no system tz database, so zoneinfo("Asia/Shanghai")
rem needs the tzdata package (Linux/macOS ship one in the OS).
"%VENV_PY%" -c "import fastapi, uvicorn, httpx, yaml, websockets, pydantic, python_multipart, tzdata; from zoneinfo import ZoneInfo; ZoneInfo('Asia/Shanghai')" 2>nul
if errorlevel 1 (
  echo [ME_Agent] ERROR: key dependencies missing ^(fastapi/uvicorn/httpx/yaml/websockets/pydantic/python-multipart/tzdata^).
  echo.
  echo Fix guidance ^(run in this folder^):
  echo   .venv\Scripts\pip.exe install -r %REQ%
  echo.
  echo Then run this script again.
  pause
  exit /b 1
)

rem ---- silent launch (start /b: no new window, logs stream here) ----
echo [ME_Agent] checks passed, launching backend silently...
start "ME_Agent" /b "%VENV_PY%" -m app %*

echo.
echo [ME_Agent] backend starting: http://127.0.0.1:8000  (console: /console/index.html)
echo Keep this window open: logs stream here. Close window or press Ctrl+C to stop.
echo.
rem Hold the console so the /b child keeps a console to live in.
pause
exit /b 0
