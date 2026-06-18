@echo off
title Setup - LinkedIn Autonomous Commenter
echo.
echo ================================================
echo   Setup: LinkedIn Autonomous Commenter
echo ================================================
echo.

echo [1/3] Installing Python packages...
pip install groq playwright python-dotenv
echo.

echo [2/3] Installing Playwright browsers...
python -m playwright install chromium
echo.

echo [3/3] Creating .env file...
if not exist ".env" (
    copy .env.example .env
    echo .env created. Please open it and add your GROQ_API_KEY
) else (
    echo .env already exists, skipping.
)

echo.
echo ================================================
echo   Setup complete!
echo.
echo   Next steps:
echo   1. Open .env and paste your Groq API key
echo   2. Run: run.bat
echo   3. First time: Log into LinkedIn in the browser
echo      (session will be saved for future runs)
echo ================================================
echo.
pause
