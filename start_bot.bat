@echo off
rem ---------------------------------------------------------------
rem  Shutsujin Bell - Kingshot countdown bot launcher
rem
rem  NOTE: keep this file ASCII-only.
rem  cmd.exe parses batch files using the system code page (CP932 here),
rem  so UTF-8 Japanese gets split into garbage and breaks parsing.
rem  Japanese messages are printed by bot.py instead, which handles
rem  the Windows console correctly on its own.
rem ---------------------------------------------------------------

chcp 65001 > nul
cd /d "%~dp0"
title Shutsujin Bell - Kingshot Countdown Bot

rem Show the bot's own messages as soon as they happen, not in chunks.
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8

if not exist ".venv\Scripts\python.exe" goto :no_venv

echo Starting the bot...
echo Press Ctrl+C or close this window to stop it.
echo.

".venv\Scripts\python.exe" bot.py

echo.
echo Bot stopped.
pause
exit /b 0

:no_venv
echo ERROR: .venv was not found.
echo.
echo Run these two commands in this folder first:
echo     python -m venv .venv
echo     .venv\Scripts\python -m pip install -r requirements.txt
echo.
pause
exit /b 1
