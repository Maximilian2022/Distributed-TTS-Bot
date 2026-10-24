@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

echo ======================================================
echo   🎙️ TTSBot Volunteer Worker (ボランティア参加ツール)
echo ======================================================
echo.

REM Pythonの存在確認
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [エラー] Python が見つかりませんでした。
    echo Python 3.9 以上をインストールし、「PATHに追加する」にチェックを入れてください。
    pause
    exit /b 1
)

REM 設定の読み込みまたは対話入力
set SERVER_URL=%~1
if "%SERVER_URL%"=="" set SERVER_URL=%TTSBOT_SERVER_URL%

if "%SERVER_URL%"=="" (
    echo 接続先の TTSBot サーバーURLを入力してください
    echo 例: wss://your-domain.com/api/worker/ws  または  ws://192.168.1.100:8080/api/worker/ws
    set /p SERVER_URL="Server URL: "
)

if "%SERVER_URL%"=="" (
    echo [エラー] 接続先サーバーURLが入力されませんでした。
    pause
    exit /b 1
)

REM http/https で入力された場合は ws/wss に自動変換
if "!SERVER_URL:~0,8!"=="https://" set SERVER_URL=wss://!SERVER_URL:~8!
if "!SERVER_URL:~0,7!"=="http://" set SERVER_URL=ws://!SERVER_URL:~7!

set TOKEN=
if not "%~2"=="" set TOKEN=%~2
if "%TOKEN%"=="" set TOKEN=%TTSBOT_TOKEN%

set NODE_NAME=%COMPUTERNAME%
if not "%~3"=="" set NODE_NAME=%~3
if not "%TTSBOT_NODE_NAME%"=="" set NODE_NAME=%TTSBOT_NODE_NAME%

if "%TOKEN%"=="" (
    echo 管理者から受け取った共有トークンを入力してください:
    set /p TOKEN="Token: "
)

echo.
echo ▶ 接続先サーバー : %SERVER_URL%
echo ▶ ノード名       : %NODE_NAME%
echo.

REM 依存ライブラリのチェック & インストール
python -c "import aiohttp" >nul 2>nul
if %errorlevel% neq 0 (
    echo ▶ 必要なライブラリ (aiohttp) をインストール中...
    pip install aiohttp
)

echo ▶ ボランティアエージェントを起動します...
python "%~dp0volunteer_worker.py" --server-url "%SERVER_URL%" --token "%TOKEN%" --name "%NODE_NAME%"

echo.
echo エージェントが停止しました。
pause
