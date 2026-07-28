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
