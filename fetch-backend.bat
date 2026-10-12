@echo off
setlocal
cd /d "%~dp0"
py -3 fetch-backend.py
if errorlevel 1 pause
