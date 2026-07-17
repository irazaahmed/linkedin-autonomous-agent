@echo off
title LinkedIn Agent Dashboard
echo.
echo ================================================
echo   LinkedIn Agent - Dashboard
echo ================================================
echo.

if not exist ".env" (
    echo [ERROR] .env file not found!
    echo Please copy .env.example to .env and add your GROQ_API_KEY
    echo.
    pause
    exit /b 1
)

start "" http://127.0.0.1:5000/
python app.py
if errorlevel 1 (
    echo.
    echo [ERROR] Dashboard crashed! Check the error above.
)

echo.
pause
