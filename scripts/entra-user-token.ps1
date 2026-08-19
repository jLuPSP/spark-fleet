$ErrorActionPreference = "Stop"

$python = Get-Command python.exe -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty Source

if (-not $python) {
    $pythonPattern = Join-Path $env:LOCALAPPDATA "Programs\Python\Python*\python.exe"
    $python = Get-ChildItem -Path $pythonPattern -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}

if (-not $python) {
    throw "Python 3 was not found. Install it or add python.exe to PATH."
}

& $python (Join-Path $PSScriptRoot "entra_user_token.py") @args
exit $LASTEXITCODE
