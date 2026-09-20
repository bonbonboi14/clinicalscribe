$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$BundledPython = "C:\Users\60162\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$Python = if (Get-Command py -ErrorAction SilentlyContinue) {
    "py"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    "python"
} elseif (Test-Path -LiteralPath $BundledPython) {
    $BundledPython
} else {
    throw "Python 3.11 or newer was not found."
}

if ($Python -eq "py") {
    & py -3.11 -m venv .venv
} else {
    & $Python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"

if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
}

& .\.venv\Scripts\python.exe scripts\init_db.py
& .\.venv\Scripts\python.exe -m pytest

