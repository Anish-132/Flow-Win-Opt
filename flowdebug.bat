@echo off
:: flow-debug.bat -- diagnostic launcher. Runs flow.py with a REAL visible
:: console (no pythonw/pyw, no `start`, no detaching) and pauses no matter
:: what, so if it's crashing before the GUI window ever appears, you get
:: to actually read the traceback instead of watching a window flash and
:: die. Once we know the real error, the fix goes into flow.py/flow.bat
:: proper and this file becomes unnecessary. Does NOT self-elevate --
:: run it from an already-elevated terminal if you need admin behavior.
setlocal
cd /d "%~dp0"

if not exist "flow.py" (
    echo flow.py not found next to this .bat.
    pause
    exit /b 1
)

where py >nul 2>&1
if %errorlevel% == 0 (
    py flow.py gui
    goto :done
)

where python >nul 2>&1
if %errorlevel% == 0 (
    python flow.py gui
    goto :done
)

echo Python not found on PATH.

:done
echo.
echo ------------------------------------------------------------
echo Exit code: %errorlevel%
echo If you saw a traceback above, copy it. If you saw NOTHING,
echo the crash is happening before Python even starts printing --
echo check that flow.py isn't truncated/corrupted.
echo ------------------------------------------------------------
pause
endlocal