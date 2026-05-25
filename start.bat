@echo off
REM Llama Manager - Windows Startup Script

setlocal enabledelayedexpansion

cd /d "%~dp0"

echo.
echo ========================================
echo Llama Manager - Startup Script
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found
    echo Please install Python 3.8 or higher
    echo Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYTHON_VERSION=%%i
echo [OK] Python version: %PYTHON_VERSION%
echo.

REM Create virtual environment if not exists
if not exist "venv" (
    echo [*] Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment
        pause
        exit /b 1
    )
)

REM Activate virtual environment
echo [*] Activating virtual environment...
call venv\Scripts\activate.bat

REM Upgrade pip
echo [*] Upgrading pip...
python -m pip install --upgrade pip -q

REM Install dependencies
if exist "requirements.txt" (
    echo [*] Installing dependencies...
    pip install -r requirements.txt -q
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies
        pause
        exit /b 1
    )
    echo [OK] Dependencies installed
) else (
    echo [ERROR] requirements.txt not found
    pause
    exit /b 1
)

echo.
echo ========================================
echo [*] Starting Llama Manager...
echo ========================================
echo.
echo Access at: http://127.0.0.1:8787
echo.
echo Press Ctrl+C to stop the service
echo.

REM Start application
python app.py

pause
