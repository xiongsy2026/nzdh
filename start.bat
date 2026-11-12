@echo off
cd /d "%~dp0"
set "PYTHONPATH=%~dp0"
set "OPEN_BROWSER=1"
title nzdh-womenswear
where python >nul 2>&1
if errorlevel 1 goto :nopython
python -c "import importlib.util as u,sys; sys.exit(0 if u.find_spec('fastapi') and u.find_spec('runninghub_sdk') and u.find_spec('playwright') else 1)" >nul 2>&1
if errorlevel 1 if exist "%~dp0vendor" (
  echo [nzdh] deps missing, offline repair from vendor...
  python -m pip install --no-index --find-links "%~dp0vendor" -r "%~dp0requirements.txt"
)
echo [nzdh] cwd=%CD%
echo [nzdh] open http://127.0.0.1:8091/womenswear
python "%~dp0app.py"
if errorlevel 1 pause
goto :eof
:nopython
echo [nzdh] python not found in PATH
pause
exit /b 1
