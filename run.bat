@echo off
setlocal enabledelayedexpansion
REM Launch from anywhere in CMD. Run from the parent of this script dir
REM so that `import instrument_app` resolves to the repo folder.

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "REPO=%%~fI"

pushd "%REPO%" >NUL
set "VENV_PY=%~dp0.venv312\Scripts\python.exe"
if exist "%VENV_PY%" (
  "%VENV_PY%" -m instrument_app %*
 ) else (
  py -3.12 -m instrument_app %*
 )
set "EC=%ERRORLEVEL%"
popd >NUL
exit /b %EC%
