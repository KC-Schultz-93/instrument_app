# Launch from anywhere on Windows PowerShell.
# Ensures we import the repo package without requiring installation.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Path $PSScriptRoot -Parent

Push-Location $repo
try {
    $venvPy = Join-Path $PSScriptRoot '.venv312\Scripts\python.exe'
    if (Test-Path $venvPy) {
        & $venvPy -m instrument_app @Args
    } else {
        py -3.12 -m instrument_app @Args
    }
}
finally {
    Pop-Location
}
