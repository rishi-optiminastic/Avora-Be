@echo off
REM Checks every prerequisite and says exactly which one is broken.
REM
REM The interpreter path is stamped in by install-windows.ps1, so these helpers
REM use exactly the same python the scheduled task does. Bare "python" is avoided
REM on purpose: on many PCs it resolves to the Windows Store stub, which exits
REM silently - whoever is diagnosing a dead connector would get a blank window and
REM learn nothing. "py -3" is the fallback before the installer has run.
cd /d "%~dp0"
set "AVORA_PY="
if exist "avora-python-path.txt" set /p AVORA_PY=<"avora-python-path.txt"
if defined AVORA_PY (
    "%AVORA_PY%" avora_biometric.py --selftest
) else (
    py -3 avora_biometric.py --selftest
)
echo.
pause
