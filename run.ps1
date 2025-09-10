py -3.12 -c "import instrument_app" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "instrument_app not found. Run from the parent folder or install with 'py -3.12 -m pip install -e instrument_app'."
    Write-Warning "From inside the package, run 'py -3.12 -m app.main'."
    exit 1
}

py -3.12 -m instrument_app @Args