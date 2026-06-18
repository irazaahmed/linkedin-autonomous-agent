@echo off
title LinkedIn Autonomous Commenter
echo.
echo ================================================
echo   LinkedIn Autonomous Commenter
echo ================================================
echo.

REM Check if .env exists
if not exist ".env" (
    echo [ERROR] .env file not found!
    echo Please copy .env.example to .env and add your GROQ_API_KEY
    echo.
    pause
    exit /b 1
)

REM Run the watcher
python linkedin_watcher.py
if errorlevel 1 (
    echo.
    echo [ERROR] Script crashed! Check the error above.
)

echo.
pause
