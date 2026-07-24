@echo off
cd /d "%~dp0"
echo ============================================
echo   BRINK'S NEXUS  -  Starting up...
echo ============================================

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  [ERROR] Virtual environment not found.
    echo  Please run setup first:
    echo.
    echo     python -m venv .venv
    echo     .venv\Scripts\activate
    echo     pip install -r requirements.txt
    echo     python -m database.seed
    echo.
    pause
    exit /b 1
)

echo  Starting backend server...
echo  Once ready, open http://127.0.0.1:8000/web/login in your browser.
.venv\Scripts\python run_backend.py

pause
