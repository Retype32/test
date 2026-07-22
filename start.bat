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
start cmd /k "cd /d "%~dp0" && .venv\Scripts\activate && python run_backend.py"

echo  Waiting for backend to initialise...
timeout /t 4 /nobreak >nul

echo  Launching desktop application...
.venv\Scripts\python main.py

pause
