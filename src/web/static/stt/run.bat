@echo off
chcp 65001 >nul
title Hayamimi Game Bridge Launcher

echo ========================================================
echo   🎙️ Hayamimi Game Bridge 起動ランチャー (Auto-Setup)
echo ========================================================
echo.

cd /d "%~dp0"

:: 1. uv (Astral uv - 高速Python環境マネージャー) の存在確認
where uv >nul 2>nul
if %errorlevel% neq 0 (
    if exist "%USERPROFILE%\.local\bin\uv.exe" (
        set "PATH=%USERPROFILE%\.local\bin;%PATH%"
    ) else if exist "%USERPROFILE%\.cargo\bin\uv.exe" (
        set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
    )
)

where uv >nul 2>nul
if %errorlevel% neq 0 (
    echo [*] uv が見つかりません。初回自動セットアップを開始します...
    echo [*] PowerShell 経由で公式インストーラーを実行中...
    powershell -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
    where uv >nul 2>nul
    if %errorlevel% neq 0 (
        echo [!] uv の自動インストールに失敗しました。手動でインストールしてください: https://docs.astral.sh/uv/
        pause
        exit /b 1
    )
    echo [OK] uv のインストールが完了しました。
)

:: 2. GitHub 経由の自動アップデート (Auto Update)
if exist "..\..\.git" (
    echo [*] GitHub リポジトリの最新更新を確認中...
    where git >nul 2>nul
    if %errorlevel% equ 0 (
        git pull --ff-only
    )
) else (
    :: スタンドアロン配布時の自動アップデート (TTSBot サーバーから直接取得)
    if exist "config.ini" (
        for /f "tokens=1,* delims==" %%A in ('type config.ini ^| findstr /i "server_url"') do (
            set "RAW_URL=%%B"
        )
    )
    if defined RAW_URL (
        set "CLEAN_URL=%RAW_URL: =%"
        set "HTTP_URL=%CLEAN_URL:ws://=http://%"
        set "HTTP_URL=%HTTP_URL:wss://=https://%"
        echo [*] 最新スクリプトを確認中: %HTTP_URL%/stt/hayamimi_game_bridge.py
        curl -s -f -o hayamimi_game_bridge.py.new "%HTTP_URL%/stt/hayamimi_game_bridge.py" >nul 2>nul
        if exist "hayamimi_game_bridge.py.new" (
            move /y hayamimi_game_bridge.py.new hayamimi_game_bridge.py >nul
            echo [OK] 最新版にアップデートされました。
        )
    )
)

:: 3. 仮想環境の準備と依存パッケージの同期
echo [*] Python 依存パッケージ (websockets, pyperclip, pyautogui) を検証中...
uv pip install -q websockets pyperclip pyautogui

:: 4. ゲームチャット連携ブリッジの実行
echo.
echo ========================================================
echo   🚀 Hayamimi Game Bridge を起動します
echo ========================================================
echo.

uv run hayamimi_game_bridge.py

echo.
echo [*] プロセスが終了しました。
pause
