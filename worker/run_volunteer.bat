@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

echo ======================================================
echo   🎙️ TTSBot Volunteer Worker (ボランティア参加ツール)
echo ======================================================
echo.

REM 1. Pythonの存在確認
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [エラー] Python が見つかりませんでした。
    echo Python 3.9 以上をインストールし、「Add Python to PATH」にチェックを入れてください。
    echo ダウンロード: https://www.python.org/downloads/
    pause
    exit /b 1
)

set SCRIPT_DIR=%~dp0
set CONFIG_FILE=%SCRIPT_DIR%config.json
set OPEN_CONFIG=0

REM 引数に --config がある場合はGUIを強制起動
if "%1"=="--config" set OPEN_CONFIG=1

REM config.json が存在しない場合はGUI設定ランチャーを起動
if not exist "%CONFIG_FILE%" set OPEN_CONFIG=1

if "%OPEN_CONFIG%"=="1" (
    echo ▶ 初回設定 GUI を起動しています...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%run_volunteer_gui.ps1" -DefaultServerUrl "%~1" -DefaultToken "%~2"
    if not exist "%CONFIG_FILE%" (
        echo 設定がキャンセルされました。
        exit /b 0
    )
)

REM 2. 依存ライブラリのチェック & インストール
python -c "import aiohttp" >nul 2>nul
if %errorlevel% neq 0 (
    echo ▶ 必要なライブラリ (aiohttp) をインストール中...
    pip install aiohttp
)

REM 3. config.json の読み込みと起動引数生成 (Pythonで安全に展開)
python -c "
import json, sys, subprocess, os

config_path = r'%CONFIG_FILE%'
if not os.path.exists(config_path):
    sys.exit(1)

with open(config_path, 'r', encoding='utf-8') as f:
    cfg = json.load(f)

server_url = cfg.get('server_url', '')
token = cfg.get('token', '')
name = cfg.get('node_name', '')
capacity = str(cfg.get('capacity', 2))
ex_guild = cfg.get('exclusive_guild_id', '')
game_priority = cfg.get('game_priority', True)

args = [sys.executable, r'%SCRIPT_DIR%volunteer_worker.py', '--server-url', server_url]
if token:
    args.extend(['--token', token])
if name:
    args.extend(['--name', name])
if capacity:
    args.extend(['--capacity', capacity])
if ex_guild:
    args.extend(['--exclusive-guild-id', ex_guild])
if game_priority:
    args.append('--game-priority')

print('▶ サーバー      : ' + server_url)
print('▶ ノード名      : ' + name)
print('▶ 提供範囲      : ' + ('専用サーバー (' + ex_guild + ')' if ex_guild else '全体共有プール'))
print('▶ ゲーム優先    : ' + ('有効 (低負荷)' if game_priority else '無効 (通常)'))
print('')
print('🎙️ ボランティアエージェントを起動します (停止は Ctrl+C またはウィンドウを閉じてください)...')

sys.exit(subprocess.call(args))
"

echo.
echo エージェントが終了しました。
pause
