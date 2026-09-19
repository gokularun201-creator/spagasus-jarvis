@echo off
title SPAGASUS JARVIS
cd /d "%~dp0"
echo Starting SPAGASUS JARVIS...
if exist python\pythonw.exe (
    set "PYW=python\pythonw.exe"
) else if exist .venv\Scripts\pythonw.exe (
    set "PYW=.venv\Scripts\pythonw.exe"
) else (
    echo Installing dependencies...
    python -m venv .venv
    .venv\Scripts\pip install -r requirements.txt
    set "PYW=.venv\Scripts\pythonw.exe"
)
if not exist .env (
    copy .env.example .env >nul
    echo Created .env - set PHONE_TOKEN and AI keys if you want them.
)

rem start the supervised server in the background (exits quietly if already running)
start "" /b %PYW% run.py

rem wait until localhost:8790 actually answers (up to 60s), then open the UI -
rem opening it before the server is ready shows a "can't connect" page
set /a tries=0
:waitloop
set /a tries+=1
if %tries% gtr 60 goto opennow
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8790/api/status -TimeoutSec 1) | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto waitloop
)
:opennow
start "" http://127.0.0.1:8790
echo SPAGASUS JARVIS is running at http://localhost:8790
