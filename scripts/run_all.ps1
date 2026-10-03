# AsOf - one-click pipeline for Windows.
# Start it by double-clicking run_all.bat in the project folder (it calls this script).
#
# What it does (each step is logged to logs\run_all.log, current step in logs\status.txt):
#   setup   find Python, create a virtual environment OUTSIDE OneDrive, install packages
#   test    run the test suite
#   doctor  check packages, Groq key, Neo4j connection and the RBI website
#   fetch   download RBI Master Directions, Master Circulars and notifications (cached, ~1-1.5 h first time)
#   build   parse -> relations -> chunks -> embeddings -> Neo4j graph
#   eval    benchmark AsOf against naive RAG
#   audit   export relations + a random sample of 50 to label by hand
#
# Run only some steps:  run_all.bat -Steps build,eval
param([string]$Steps = "setup,test,doctor,fetch,build,eval,audit")

$ErrorActionPreference = "Continue"
$Project = Split-Path -Parent $PSScriptRoot
Set-Location $Project

$Logs = Join-Path $Project "logs"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
$Log = Join-Path $Logs "run_all.log"
$Status = Join-Path $Logs "status.txt"

# Heavy files live outside OneDrive so syncing does not slow everything down.
$Home2 = Join-Path $env:LOCALAPPDATA "asof"
$Venv = Join-Path $Home2 "venv"
$VPy = Join-Path $Venv "Scripts\python.exe"
$env:ASOF_DATA_DIR = Join-Path $Home2 "data"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
$Want = $Steps.ToLower().Split(",") | ForEach-Object { $_.Trim() }

function Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line -ForegroundColor Cyan
    Add-Content -Path $Log -Value $line -Encoding UTF8
}
function Set-Status([string]$s) {
    Set-Content -Path $Status -Value ("{0} | {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $s) -Encoding UTF8
}
function Invoke-Step([string]$name, [scriptblock]$cmd) {
    Log "START $name"
    Set-Status "RUNNING $name"
    & $cmd 2>&1 | ForEach-Object {
        $t = "$_"
        Write-Host $t
        Add-Content -Path $Log -Value $t -Encoding UTF8
    }
    $code = $LASTEXITCODE
    Log "END $name (exit code $code)"
    return $code
}

# Keep the laptop awake while this window runs (no settings are changed; this ends when the window closes).
try {
    Add-Type -Namespace AsOf -Name Power -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);'
    [AsOf.Power]::SetThreadExecutionState([uint32]"0x80000001") | Out-Null
} catch { }

Log "==== AsOf run started. Steps: $Steps ===="
Log "Project folder: $Project"
Log "Virtual env:    $Venv"
Log "Data folder:    $env:ASOF_DATA_DIR"
if (-not (Test-Path (Join-Path $Project ".env"))) { Log "WARNING: .env not found in the project folder" }

# ---------------- setup ----------------
if ($Want -contains "setup") {
    Set-Status "RUNNING setup"
    $PyExe = $null; $PyArgs = @()
    foreach ($c in @("py -3.11", "py -3.12", "py -3.10", "py -3.13", "python")) {
        $parts = $c.Split(" ")
        $exe = $parts[0]
        $rest = @()
        if ($parts.Length -gt 1) { $rest = $parts[1..($parts.Length - 1)] }
        try { $ver = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null } catch { continue }
        if ($LASTEXITCODE -eq 0 -and "$ver" -match '^3\.(10|11|12|13)$') { $PyExe = $exe; $PyArgs = $rest; Log "Using Python $ver ($c)"; break }
    }
    if (-not $PyExe) {
        Log "FAILED: no Python 3.10-3.13 found. Install Python 3.11 from python.org (tick 'Add python.exe to PATH') and run again."
        Set-Status "FAILED setup: Python not found"
        exit 1
    }
    if (-not (Test-Path $VPy)) {
        New-Item -ItemType Directory -Force -Path $Home2 | Out-Null
        $code = Invoke-Step "create venv" { & $PyExe @PyArgs -m venv $Venv }
        if ($code -ne 0) { Set-Status "FAILED setup: venv"; exit 1 }
    }
    Invoke-Step "upgrade pip" { & $VPy -m pip install --upgrade pip --disable-pip-version-check } | Out-Null
    $code = Invoke-Step "install requirements" { & $VPy -m pip install -r requirements.txt --disable-pip-version-check }
    if ($code -ne 0) { Set-Status "FAILED setup: pip install"; exit 1 }
    $code = Invoke-Step "install project" { & $VPy -m pip install -e . --disable-pip-version-check }
    if ($code -ne 0) { Set-Status "FAILED setup: pip install -e"; exit 1 }
}
if (-not (Test-Path $VPy)) { Log "FAILED: virtual env missing - run the setup step first"; Set-Status "FAILED: no venv"; exit 1 }

# ---------------- test ----------------
if ($Want -contains "test") {
    $code = Invoke-Step "tests" { & $VPy -m pytest -q }
    if ($code -ne 0) { Log "Tests failed - continuing so the log shows everything" }
}

# ---------------- doctor ----------------
if ($Want -contains "doctor") {
    Invoke-Step "doctor" { & $VPy -m asof.cli doctor } | Out-Null
}

# ---------------- fetch ----------------
if ($Want -contains "fetch") {
    Invoke-Step "fetch master directions" { & $VPy -m asof.cli fetch --source md } | Out-Null
    Invoke-Step "fetch master circulars" { & $VPy -m asof.cli fetch --source mc } | Out-Null
    Invoke-Step "fetch notifications 11300-13720" { & $VPy -m asof.cli fetch --source ids --start 11300 --end 13720 } | Out-Null
    $raw = Join-Path $env:ASOF_DATA_DIR "raw"
    $n = (Get-ChildItem -Path $raw -Filter *.html -ErrorAction SilentlyContinue | Measure-Object).Count
    Log "Downloaded pages in cache: $n"
    # copy a few sample pages into the project so they can be inspected
    $samples = Join-Path $Logs "samples"
    New-Item -ItemType Directory -Force -Path $samples | Out-Null
    foreach ($pat in @("md_*.html", "mc_*.html", "ids_*.html")) {
        Get-ChildItem -Path $raw -Filter $pat -ErrorAction SilentlyContinue | Select-Object -First 3 | ForEach-Object {
            Copy-Item $_.FullName -Destination $samples -Force
            $meta = [System.IO.Path]::ChangeExtension($_.FullName, ".json")
            if (Test-Path $meta) { Copy-Item $meta -Destination $samples -Force }
        }
    }
}

# ---------------- build ----------------
if ($Want -contains "build") {
    $code = Invoke-Step "build index" { & $VPy -m asof.cli build }
    if ($code -ne 0) { Set-Status "FAILED build"; exit 1 }
}

# ---------------- eval ----------------
if ($Want -contains "eval") {
    Invoke-Step "evaluation" { & $VPy -m asof.cli eval --regenerate } | Out-Null
    $res = Join-Path $env:ASOF_DATA_DIR "eval\results.md"
    if (Test-Path $res) { Copy-Item $res -Destination (Join-Path $Logs "results.md") -Force }
}

# ---------------- audit ----------------
if ($Want -contains "audit") {
    Invoke-Step "relation audit export" { & $VPy -m asof.cli audit } | Out-Null
    $evalDir = Join-Path $Project "data\eval"
    New-Item -ItemType Directory -Force -Path $evalDir | Out-Null
    foreach ($f in @("relations_audit.csv", "relations_audit_sample50.csv")) {
        $src = Join-Path $env:ASOF_DATA_DIR "eval\$f"
        if (Test-Path $src) { Copy-Item $src -Destination $evalDir -Force }
    }
}

Log "==== AsOf run finished ===="
Set-Status "DONE ($Steps)"
