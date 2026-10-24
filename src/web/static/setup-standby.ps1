# ==============================================================================
# TTSBot Manager Slave (Standby) Windows One-Liner Auto Setup Script
# ==============================================================================
# 使い方:
#   irm https://<あなたのTTSBotドメイン>/setup-standby.ps1 | iex
#   または引数指定:
#   .\setup-standby.ps1 -MasterUrl "https://ttsbot.example.com" -HaSecret "secret123"
# ==============================================================================

param(
    [string]$MasterUrl = "",
    [string]$HaSecret = "",
    [string]$TunnelToken = ""
)

$ErrorActionPreference = "Stop"

Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "  🎙️ TTSBot Manager Slave (Standby) 自動セットアップ (Windows)" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan

# 1. 配信元 Master サーバーから自動埋め込みされた規定値
$EmbeddedMasterUrl = "__DEFAULT_MASTER_URL__"
$EmbeddedHaSecret = "__DEFAULT_HA_SECRET__"

if ($EmbeddedMasterUrl -like "*__DEFAULT*") { $EmbeddedMasterUrl = "" }
if ($EmbeddedHaSecret -like "*__DEFAULT*") { $EmbeddedHaSecret = "" }

# 引数 -> 環境変数 -> 配信元埋め込み値 の順に優先度判定
if (-not $MasterUrl -or $MasterUrl -eq "__DEFAULT_MASTER_URL__") {
    if ($env:TTSBOT_MASTER_URL) {
        $MasterUrl = $env:TTSBOT_MASTER_URL
    } elseif ($EmbeddedMasterUrl) {
        $MasterUrl = $EmbeddedMasterUrl
    } else {
        $MasterUrl = Read-Host "Master (主系) サーバーのベースURL (例: https://ttsbot.example.com)"
    }
}
$MasterUrl = $MasterUrl.TrimEnd("/")

if (-not $HaSecret -or $HaSecret -eq "__DEFAULT_HA_SECRET__") {
    if ($env:TTSBOT_HA_SECRET) {
        $HaSecret = $env:TTSBOT_HA_SECRET
    } elseif ($EmbeddedHaSecret) {
        $HaSecret = $EmbeddedHaSecret
    } else {
        $HaSecret = Read-Host "HA 共有認証シークレットキー (TTSBOT_HA_SECRET)"
    }
}

if (-not $TunnelToken -and $env:CLOUDFLARE_TUNNEL_TOKEN) {
    $TunnelToken = $env:CLOUDFLARE_TUNNEL_TOKEN
}

Write-Host "  ✓ 接続先 Master URL: $MasterUrl" -ForegroundColor Green
Write-Host "  ✓ HA 認証シークレット: 設定完了 (自動認証・暗号化同期)" -ForegroundColor Green

# 2. Docker 稼働チェック
Write-Host "`n[1/4] Docker Desktop 環境の確認中..." -ForegroundColor Yellow
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCmd) {
    Write-Host "❌ エラー: docker コマンドが見つかりません。" -ForegroundColor Red
    Write-Host "Docker Desktop for Windows をインストールしてから再度実行してください: https://www.docker.com/products/docker-desktop/"
    exit 1
}

try {
    $dockerInfo = docker info 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ エラー: Docker デーモンが起動していません。Docker Desktop を起動してください。" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "❌ エラー: Docker デーモンへの接続に失敗しました。" -ForegroundColor Red
    exit 1
}

Write-Host "  ✓ Docker Desktop: 稼働中" -ForegroundColor Green

# 3. Master への疎通テスト
Write-Host "`n[2/4] Master サーバー ($MasterUrl) への疎通確認中..." -ForegroundColor Yellow
try {
    $healthResp = Invoke-RestMethod -Uri "$MasterUrl/health" -TimeoutSec 5 -ErrorAction Stop
    if ($healthResp.status -eq "ok") {
        Write-Host "  ✓ Master /health 応答: 正常 (200 OK)" -ForegroundColor Green
    }
} catch {
    Write-Host "  ⚠️ 警告: Master ($MasterUrl/health) から応答がありません。" -ForegroundColor Yellow
    Write-Host "  URL が正しいか確認してください（セットアップは継続します）。"
}

# 4. インストール先ディレクトリの準備
$InstallDir = Join-Path $env:USERPROFILE "ttsbot-slave"
Write-Host "`n[3/4] Slave 設定・ディレクトリのセットアップ ($InstallDir)..." -ForegroundColor Yellow

if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}
Set-Location $InstallDir

# リポジトリ全体 (Dockerfile やソースコード一式) の確実な取得
if (-not (Test-Path (Join-Path $InstallDir "Dockerfile"))) {
    $cloned = $false
    $gitCmd = Get-Command git -ErrorAction SilentlyContinue
    if ($gitCmd) {
        Write-Host "  リポジトリを取得中 (git)..."
        try {
            if (-not (Test-Path (Join-Path $InstallDir ".git"))) {
                git clone https://github.com/Maximilian2022/Distributed-TTS-Bot.git . 2>$null
                if (Test-Path (Join-Path $InstallDir "Dockerfile")) { $cloned = $true }
            } else {
                git fetch --depth=1 origin main 2>$null
                git reset --hard origin/main 2>$null
                if (Test-Path (Join-Path $InstallDir "Dockerfile")) { $cloned = $true }
            }
        } catch {}
    }

    if (-not $cloned -and -not (Test-Path (Join-Path $InstallDir "Dockerfile"))) {
        Write-Host "  GitHub からソースコード一式 (zip) を取得・展開中..."
        try {
            $zipPath = Join-Path $env:TEMP "ttsbot-main.zip"
            $extractPath = Join-Path $env:TEMP "ttsbot-extract"
            Invoke-WebRequest -Uri "https://github.com/Maximilian2022/Distributed-TTS-Bot/archive/refs/heads/main.zip" -OutFile $zipPath -TimeoutSec 30
            if (Test-Path $extractPath) { Remove-Item -Path $extractPath -Recurse -Force -ErrorAction SilentlyContinue }
            Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force
            $innerDir = Join-Path $extractPath "Distributed-TTS-Bot-main"
            if (Test-Path $innerDir) {
                Copy-Item -Path "$innerDir\*" -Destination $InstallDir -Recurse -Force
                $cloned = $true
            }
            Remove-Item -Path $zipPath -Force -ErrorAction SilentlyContinue
            Remove-Item -Path $extractPath -Recurse -Force -ErrorAction SilentlyContinue
        } catch {}
    }

    if (-not (Test-Path (Join-Path $InstallDir "docker-compose.yml"))) {
        Write-Host "  Master から docker-compose.yml を取得中..."
        try {
            Invoke-WebRequest -Uri "$MasterUrl/docker-compose.yml" -OutFile (Join-Path $InstallDir "docker-compose.yml") -TimeoutSec 10 -ErrorAction SilentlyContinue
        } catch {}
    }
}

if (-not (Test-Path (Join-Path $InstallDir "Dockerfile"))) {
    Write-Host "❌ エラー: Dockerfile を取得できませんでした。GitHub へのアクセスを確認してください。" -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Path (Join-Path $InstallDir "data") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $InstallDir "logs") -Force | Out-Null

# .env ファイルの生成
$EnvFile = Join-Path $InstallDir ".env"
Write-Host "  .env 設定ファイルを生成中..."

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$envContent = @"
# ==============================================================================
# TTSBot Slave (Standby) 環境設定 (Windows)
# 生成日時: $timestamp
# ==============================================================================

# HA ロール設定
TTSBOT_HA_ROLE=slave
TTSBOT_MASTER_URL=$MasterUrl
TTSBOT_HA_SECRET=$HaSecret
TTSBOT_CHECK_INTERVAL=5
TTSBOT_FAIL_THRESHOLD=6
TTSBOT_SYNC_INTERVAL=60

# Docker Hub イメージ指定 (未指定時は madpiero/ttsbot:latest)
TTSBOT_IMAGE=madpiero/ttsbot:latest

# Web & ポート設定
HOST_WEB_PORT=8088
WEB_PORT=8080

# Cloudflare Tunnel (任意)
CLOUDFLARE_TUNNEL_TOKEN=$TunnelToken

"@

Set-Content -Path $EnvFile -Value $envContent -Encoding UTF8
Write-Host "  ✓ .env ファイル作成完了: $EnvFile" -ForegroundColor Green

# 5. Standby Watchdog コンテナの起動 (--profile standby 指定)
Write-Host "`n[4/4] Slave Standby Watchdog を起動中..." -ForegroundColor Yellow

# Docker Hub から最新ビルド済みイメージを高速プル
Write-Host "  Docker Hub から最新イメージを確認・取得中..."
docker compose --profile standby pull standby-watchdog 2>$null

docker compose --profile standby up -d standby-watchdog

Write-Host "`n======================================================" -ForegroundColor Green
Write-Host "  🎉 TTSBot Slave (Standby) のセットアップが完了しました！" -ForegroundColor Green
Write-Host "======================================================" -ForegroundColor Green
Write-Host "  ・動作モード   : Slave (Standby 待機・自動同期)" -ForegroundColor Cyan
Write-Host "  ・監視対象     : $MasterUrl" -ForegroundColor Cyan
Write-Host "  ・管理コマンド :"
Write-Host "     - ログ確認 : cd $InstallDir; docker compose --profile standby logs -f standby-watchdog" -ForegroundColor White
Write-Host "     - 停止     : cd $InstallDir; docker compose --profile standby stop standby-watchdog" -ForegroundColor White
Write-Host "     - 再開     : cd $InstallDir; docker compose --profile standby up -d standby-watchdog" -ForegroundColor White
Write-Host "`n💡 Master がダウンした際は自動的に Active に昇格し、Master が復旧すると自動的に Standby に戻ります。`n"
