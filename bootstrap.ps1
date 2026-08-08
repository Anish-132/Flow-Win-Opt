<#
Flow — one-line installer/launcher.

Usage:
  irm https://raw.githubusercontent.com/Anish-132/Flow-Win-Opt/main/bootstrap.ps1 | iex

What this does, in order:
  1. Creates a GUID-named folder under %TEMP%
  2. Downloads flow.py, flow.bat, requirements.txt from the main branch
  3. Launches flow.bat already elevated (one UAC prompt) so it runs
     straight through instead of re-launching itself
  4. Waits for the Flow window to close
  5. Deletes the temp folder

Nothing is left on disk after you close the window. Read this file before
piping anything to `iex`, including this one — that's good practice for
any script fetched over `irm | iex`, not just this project's.
#>

$ErrorActionPreference = "Stop"

# ANSI Shadow-style block banner, same style WinUtil/christitus.com uses.
# Printed first, before any downloading/elevation happens, so it shows up
# immediately whether this is run via `irm | iex` or as a saved .ps1 --
# and works from a plain PowerShell prompt, no external figlet dependency.
$banner = @"
███████╗██╗      ██████╗ ██╗    ██╗
██╔════╝██║     ██╔═══██╗██║    ██║
█████╗  ██║     ██║   ██║██║ █╗ ██║
██╔══╝  ██║     ██║   ██║██║███╗██║
██║     ███████╗╚██████╔╝╚███╔███╔╝
╚═╝     ╚══════╝ ╚═════╝  ╚══╝╚══╝
"@
Write-Host $banner -ForegroundColor Cyan
Write-Host "  Windows System Optimizer" -ForegroundColor DarkCyan
Write-Host "  github.com/Anish-132/Flow-Win-Opt`n" -ForegroundColor DarkGray

function Test-RealPython {
    # Windows ships a fake "python" stub (App Execution Alias) that opens
    # the Store even when no interpreter is installed. Check real output.
    try {
        $out = & python --version 2>&1
        if ($LASTEXITCODE -eq 0 -and $out -match "Python \d") { return $true }
    } catch {}
    return $false
}

if (-not (Test-RealPython)) {
    Write-Host "Python not found — installing via winget..." -ForegroundColor Yellow
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "winget is not available. Install Python manually from python.org (check 'Add to PATH'), then re-run this script." -ForegroundColor Red
        exit 1
    }
    winget install -e --id Python.Python.3.12 --scope machine --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Python install failed. Install manually from python.org, then re-run this script." -ForegroundColor Red
        exit 1
    }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    if (-not (Test-RealPython)) {
        Write-Host "Python installed but not on PATH yet. Close this window, reopen PowerShell, and re-run bootstrap." -ForegroundColor Red
        exit 1
    }
    Write-Host "Python installed OK." -ForegroundColor Green
} else {
    Write-Host "Python found." -ForegroundColor Green
}

$repoRaw = "https://raw.githubusercontent.com/Anish-132/Flow-Win-Opt/main"
$tempDir = Join-Path $env:TEMP ("flow_" + [guid]::NewGuid().ToString("N"))

Write-Host "Flow — downloading to a temp folder..." -ForegroundColor Cyan
New-Item -ItemType Directory -Path $tempDir | Out-Null

$files = @("flow.py", "flow.bat", "requirements.txt")
foreach ($f in $files) {
    Write-Host "  fetching $f"
    Invoke-WebRequest -Uri "$repoRaw/$f" -OutFile (Join-Path $tempDir $f) -UseBasicParsing
}

Write-Host "Launching Flow (UAC prompt is normal — registry/service tweaks need admin)..." -ForegroundColor Cyan
try {
    # Launch already-elevated so flow.bat's own self-elevation check sees an
    # active admin session and runs straight through, instead of relaunching
    # itself and exiting this process early.
    Start-Process -FilePath (Join-Path $tempDir "flow.bat") -WorkingDirectory $tempDir -Verb RunAs -Wait
}
finally {
    Write-Host "Cleaning up temp folder..." -ForegroundColor Cyan
    Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Done." -ForegroundColor Green