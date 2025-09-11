@echo off
py -3.12 -c "import instrument_app" >NUL 2>&1
if errorlevel 1 (
    echo instrument_app not found. Run from the repository root or install with "py -3.12 -m pip install -e instrument_app".
    exit /b 1
)
py -3.12 -m instrument_app %*