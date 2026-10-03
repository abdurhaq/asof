# AsOf quick start for Windows PowerShell.
# Run from the project folder:   powershell -ExecutionPolicy Bypass -File scripts\quickstart.ps1
$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    python -m venv .venv
}
. .\.venv\Scripts\Activate.ps1
$env:PYTHONPATH = "$PWD\src"

Write-Host "Installing packages (first time takes a few minutes)..." -ForegroundColor Cyan
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .

if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Write-Host "Created .env - open it and paste your GROQ_API_KEY (and Neo4j details if you use AuraDB)." -ForegroundColor Yellow
}

Write-Host "Running tests..." -ForegroundColor Cyan
python -m pytest -q

Write-Host "Downloading RBI documents (cached; safe to re-run)..." -ForegroundColor Cyan
python -m asof.cli fetch --source md
python -m asof.cli fetch --source mc

Write-Host "Building the index..." -ForegroundColor Cyan
python -m asof.cli build

Write-Host "Running the benchmark (retrieval metrics, no API calls)..." -ForegroundColor Cyan
python -m asof.cli eval

Write-Host "Starting the app at http://127.0.0.1:8000 ..." -ForegroundColor Green
python -m asof.cli serve
