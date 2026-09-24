@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python virtual environment not found: .venv\Scripts\python.exe
    echo Create it with: python -m venv .venv
    echo Then install dependencies with: .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo Starting EComSpider API on all interfaces at port 8000.
echo Local docs: http://127.0.0.1:8000/docs
echo Press Ctrl+C to stop the service.
echo.
".venv\Scripts\python.exe" run_api.py

if errorlevel 1 (
    echo.
    echo [ERROR] API stopped with exit code %errorlevel%.
    pause
)

endlocal
