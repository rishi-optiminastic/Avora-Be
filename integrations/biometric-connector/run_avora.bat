@echo off
REM Runs the Avora biometric connector in a loop, logging to avora_biometric.log.
REM Point Windows Task Scheduler at this file (trigger: At startup) so it survives
REM terminal-close, logoff, and reboot. Keep this .bat next to avora_biometric.py.
cd /d "%~dp0"
python avora_biometric.py --loop 300 >> avora_biometric.log 2>&1
