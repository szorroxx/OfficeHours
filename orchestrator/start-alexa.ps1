Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Virtual environment not found. Create it with: py -m venv .venv"
}

$env:MODE = if ($env:MODE) { $env:MODE } else { "mock" }
$port = if ($env:PORT) { $env:PORT } else { "8001" }

& ".venv\Scripts\python.exe" -m uvicorn server:app --host 0.0.0.0 --port $port --reload
