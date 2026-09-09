@echo off
chcp 65001 > nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 仮想環境が見つかりません。先に次を実行してください:
    echo     python -m venv .venv
    echo     .venv\Scripts\python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo 出陣ベルを起動します。
echo 止めるときは Ctrl+C を押すか、このウィンドウを閉じてください。
echo.

".venv\Scripts\python.exe" bot.py

echo.
echo 終了しました。
pause
