@echo off
:: flow.bat -- launches Flow elevated (UAC), since Section 4's registry and
:: service tweaks need admin rights to actually write anything. Without
:: this, Flow still opens fine but sits in "not elevated" / dry-run mode
:: forever, per the admin_status() banner in the GUI -- this script exists
:: purely so double-clicking one file gets you the working state.
::
:: Self-elevation pattern: check for an active admin session (`net session`
:: fails silently and harmlessly if you're not admin -- it doesn't create
:: or modify anything, just queries). If not admin, relaunch THIS SAME
:: .bat via PowerShell's Start-Process -Verb RunAs, which triggers the
:: standard UAC prompt, then exit the original non-elevated copy. The
:: relaunched copy re-runs this whole script, passes the admin check the
:: second time, and falls through to actually starting Flow.

setlocal

net session >nul 2>&1
if %errorlevel% == 0 goto :run

echo Flow needs administrator rights to apply system tweaks.
echo Requesting elevation via UAC...

:: If this script lives on a mapped network drive (e.g. P:\...), the
:: elevated process launched by UAC does NOT inherit mapped drive letters
:: from this session -- they're per-user-logon, not machine-wide, so an
:: elevated relaunch pointed at "P:\..." can't resolve the drive at all
:: and dies instantly/silently. That's the "flashes and disappears"
:: symptom. Fix: resolve the drive to its real UNC path first and elevate
:: using THAT instead, which elevated processes can always see.
set "ELEVTARGET=%~f0"
set "DRIVE=%~d0"
for /f "tokens=3" %%U in ('reg query "HKCU\Network\%DRIVE:~0,1%" /v RemotePath 2^>nul ^| findstr /i RemotePath') do set "UNCROOT=%%U"
if defined UNCROOT (
    set "ELEVTARGET=%UNCROOT%%~pnx0"
)

powershell -NoProfile -Command "Start-Process -FilePath '%ELEVTARGET%' -Verb RunAs"
exit /b 0

:run
:: Run from this script's own folder regardless of where it was launched
:: from (double-click, shortcut, another cwd in a terminal) -- matters for
:: _flow_deps/ vendoring and for finding .env next to flow.py.
pushd "%~dp0"

if not exist "flow.py" (
    echo flow.py not found next to flow.bat -- keep them in the same folder.
    pause
    exit /b 1
)

:: Running with a normal visible console on purpose now -- the windowless
:: pyw/pythonw + start-detach approach kept producing silent failures that
:: were impossible to diagnose (mapped-drive UAC issue, attribute bugs,
:: etc all hid behind a vanishing window). A console box staying open
:: behind the GUI is a minor cosmetic cost; a launcher that can't show
:: you why it failed is a much bigger problem. Revisit windowless launch
:: later once the app itself is solid.
where py >nul 2>&1
if %errorlevel% == 0 (
    py flow.py gui
    goto :donerun
)

where python >nul 2>&1
if %errorlevel% == 0 (
    python flow.py gui
    goto :donerun
)

:: Neither resolves on PATH at all -- last resort before giving up: probe
:: the handful of locations python.org's installer actually uses (per-user
:: default under LocalAppData\Programs, and the machine-wide Program Files
:: path), since a fresh install with "Add to PATH" unchecked still leaves
:: a perfectly working interpreter sitting right there.
for %%V in (314 313 312 311 310 39) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" flow.py gui
        goto :donerun
    )
    if exist "C:\Program Files\Python%%V\python.exe" (
        "C:\Program Files\Python%%V\python.exe" flow.py gui
        goto :donerun
    )
)

echo Python not found. Neither "python" nor "py" resolves on PATH, and no
echo install was found in the usual python.org locations either.
echo Install Python 3 from https://python.org -- check "Add python.exe to
echo PATH" during setup -- then run flow.bat again.
pause
exit /b 1

:donerun

echo.
echo ------------------------------------------------------------
echo Flow exited (code %errorlevel%). Window stays open so you can
echo read any output above. Close this window or press a key.
echo ------------------------------------------------------------
pause

endlocal