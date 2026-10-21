# ==============================================================================
# TTSBot Compute Worker (Windows / Docker Desktop) 自動セットアップスクリプト v3
#
# 実行方法 (Windows PowerShell 管理者権限):
#   irm https://ttsbot.kenko.land/setup-worker.ps1 | iex
#
# 機能:
#   - 3大TTSエンジン (VOICEVOX / SHAREVOX / COEIROINK) の自動ビルド・起動
#   - GPU (NVIDIA) / CPU / RAM の自動検出 → リソース制限・並列数を動的最適化
#   - 192.168.11.0/24 サブネット優先の正確な物理LAN IP自動検出 (仮想NIC除外)
#   - Worker Agent コンテナを起動し Master に自動登録 & ハートビート
#   - 自己アップデート機能（WebUI から /update エンドポイント経由）
# ==============================================================================

$MasterUrl  = if ($env:TTSBOT_MASTER_URL) { $env:TTSBOT_MASTER_URL } else { "http://192.168.11.245:8088" }
$WorkDir    = if ($env:TTSBOT_WORK_DIR)   { $env:TTSBOT_WORK_DIR   } else { "$HOME\ttsbot-worker" }
$WorkDirUnix = $WorkDir.Replace('\', '/')

Write-Host "==============================================================================" -ForegroundColor Cyan
Write-Host "   🚀 TTSBot Compute Worker 自動セットアップ (Windows版) v3" -ForegroundColor Cyan
Write-Host "   Master: $MasterUrl" -ForegroundColor Cyan
Write-Host "==============================================================================" -ForegroundColor Cyan
Write-Host "ℹ️  作業ディレクトリ: $WorkDir" -ForegroundColor Blue

New-Item -ItemType Directory -Force -Path "$WorkDir\docker\sharevox" | Out-Null
New-Item -ItemType Directory -Force -Path "$WorkDir\docker\coeiroink" | Out-Null
New-Item -ItemType Directory -Force -Path "$WorkDir\docker\agent"     | Out-Null
New-Item -ItemType Directory -Force -Path "$WorkDir\coeiroink_models" | Out-Null
Set-Location $WorkDir

# 1. Docker チェック
Write-Host "`n🔍 [1/5] Docker Desktop の動作状況をチェック中..." -ForegroundColor Blue
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "❌ エラー: Docker がインストールされていないか PATH が通っていません。" -ForegroundColor Red
    Write-Host "   Docker Desktop for Windows をインストールし、起動してから再実行してください。" -ForegroundColor Yellow
    exit 1
}
try {
    docker info | Out-Null
    Write-Host "✅ Docker Desktop が起動しています。" -ForegroundColor Green
} catch {
    Write-Host "❌ Docker Desktop が起動していません。起動してから再実行してください。" -ForegroundColor Red
    exit 1
}

# 2. ハードウェアスペック自動検出
Write-Host "`n🖥️  [2/5] ハードウェアスペックを自動検出中..." -ForegroundColor Blue

# CPU コア数 (共用マシン配慮: 半分を使用)
$TotalCpus = (Get-WmiObject Win32_ComputerSystem).NumberOfLogicalProcessors
$AllocCpus = [Math]::Max(1, [Math]::Floor($TotalCpus / 2))

# RAM (共用配慮: 半分を使用)
try {
    $TotalRamBytes = (Get-WmiObject Win32_ComputerSystem).TotalPhysicalMemory
    $TotalRamGb    = [Math]::Round($TotalRamBytes / 1GB, 1)
    $AllocRamGb    = [Math]::Max(2, [Math]::Floor($TotalRamGb / 2))
} catch {
    $TotalRamGb = 8; $AllocRamGb = 4
}

# 各エンジンへのRAM割り当て
$RamVv = [Math]::Max(1, [int]($AllocRamGb * 0.35))
$RamSv = [Math]::Max(1, [int]($AllocRamGb * 0.30))
$RamCi = [Math]::Max(1, [int]($AllocRamGb * 0.35))

# GPU 検出 (nvidia-smi)
$HasGpu    = $false
$GpuName   = ""
$GpuVramMb = 0
try {
    $NvidiaSmiOut = nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null
    if ($LASTEXITCODE -eq 0 -and $NvidiaSmiOut) {
        $Parts     = $NvidiaSmiOut -split ","
        $HasGpu    = $true
        $GpuName   = $Parts[0].Trim()
        $GpuVramMb = [int]$Parts[1].Trim()
        Write-Host "  ✅ GPU 検出: $GpuName ($GpuVramMb MB VRAM)" -ForegroundColor Green
    }
} catch { }

# スレッド数
$ThreadNum = [Math]::Max(1, $AllocCpus)

# 動的並列数計算
if ($HasGpu -and $GpuVramMb -ge 8000) {
    $MaxConcurrent = [Math]::Min(3, [Math]::Max(1, [int]($GpuVramMb / 4000)))
    $Priority      = 1
} elseif ($HasGpu -and $GpuVramMb -ge 4000) {
    $MaxConcurrent = 2
    $Priority      = 1
} elseif ($TotalRamGb -ge 16) {
    $MaxConcurrent = 2
    $Priority      = 2
} else {
    $MaxConcurrent = 1
    $Priority      = 2
}

# VOICEVOX イメージ選択
$VoicevoxImage = if ($HasGpu) { "voicevox/voicevox_engine:nvidia-ubuntu20.04-latest" } else { "voicevox/voicevox_engine:cpu-ubuntu20.04-latest" }

# COEIROINK 設定 (GPU / CPU 動的切り替え)
if ($HasGpu) {
    $CoeiroinkDir       = "COEIROINK_LINUX_GPU_v.2025.4.1"
    $CoeiroinkZipUrl   = "https://www.dropbox.com/scl/fi/pisvh3pwlinz6hdep2fcr/COEIROINK_LINUX_GPU_v.2025.4.1.zip?rlkey=4dg1368zzdbplahcjdbqoj2tg&st=3k7yonjs&dl=1"
    $CoeiroinkBaseImage = "nvidia/cuda:11.8.0-base-ubuntu22.04"
} else {
    $CoeiroinkDir       = "COEIROINK_LINUX_CPU_v.2025.4.1"
    $CoeiroinkZipUrl   = "https://www.dropbox.com/scl/fi/o7xnjfo0jw81otgn2jxar/COEIROINK_LINUX_CPU_v.2025.4.1.zip?rlkey=dt1z2bxrbo3ikm2iiioxh9wx7&st=tso7516f&dl=1"
    $CoeiroinkBaseImage = "ubuntu:22.04"
}

Write-Host "  CPU: $TotalCpus コア → 割り当て: $AllocCpus コア / $ThreadNum スレッド" -ForegroundColor Blue
Write-Host "  RAM: $TotalRamGb GB → 割り当て上限計: $AllocRamGb GB (VV:${RamVv}G, SV:${RamSv}G, CI:${RamCi}G)" -ForegroundColor Blue
Write-Host "  GPU: $(if ($HasGpu) { 'あり (' + $GpuName + ')' } else { 'なし (CPU モード)' })" -ForegroundColor Blue
Write-Host "  推奨並列数: $MaxConcurrent / 優先度: $Priority" -ForegroundColor Blue

# IP アドレスの正確な検出 (192.168.11.0/24 優先、VMware/VirtualBox/WSL等の仮想NICを除外)
$LocalIp = ""
# 1. 192.168.11.* を最優先で検索
$MatchedIps = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {
    $_.IPAddress -like "192.168.11.*"
}
if ($MatchedIps) {
    $LocalIp = ($MatchedIps | Select-Object -First 1).IPAddress
}

# 2. デフォルトゲートウェイ（0.0.0.0/0）を持つ物理インターフェースから取得
if (-not $LocalIp) {
    $DefaultRoute = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric | Select-Object -First 1
    if ($DefaultRoute) {
        $IpObj = Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $DefaultRoute.InterfaceIndex -ErrorAction SilentlyContinue |
            Where-Object {
                $_.IPAddress -notlike "127.*" -and
                $_.IPAddress -notlike "169.254.*" -and
                $_.InterfaceAlias -notlike "*VMware*" -and
                $_.InterfaceAlias -notlike "*VirtualBox*" -and
                $_.InterfaceAlias -notlike "*WSL*" -and
                $_.InterfaceAlias -notlike "*vEthernet*"
            } | Select-Object -First 1
        if ($IpObj) { $LocalIp = $IpObj.IPAddress }
    }
}

# 3. 仮想NICを除外して最初のアクティブIPを取得
if (-not $LocalIp) {
    $IpObj = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {
        $_.InterfaceAlias -notlike "*Loopback*" -and
        $_.InterfaceAlias -notlike "*vEthernet*" -and
        $_.InterfaceAlias -notlike "*VMware*" -and
        $_.InterfaceAlias -notlike "*VirtualBox*" -and
        $_.InterfaceAlias -notlike "*WSL*" -and
        $_.InterfaceAlias -notlike "*Bluetooth*" -and
        $_.InterfaceAlias -notlike "*TAP*" -and
        $_.IPAddress -notlike "127.*" -and
        $_.IPAddress -notlike "169.254.*"
    } | Select-Object -First 1
    if ($IpObj) { $LocalIp = $IpObj.IPAddress }
}

if (-not $LocalIp) { $LocalIp = "127.0.0.1" }
Write-Host "  検出 LAN IP: $LocalIp" -ForegroundColor Green

# 3. 構成ファイル展開
Write-Host "`n📦 [3/5] コンテナ構成ファイルを展開中..." -ForegroundColor Blue

# Dockerfile 類の書き出し (UTF8 / LF 改行)
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

# SHAREVOX Dockerfile (GPUあり時は公式GPUイメージ+モデル注入、CPU時は軽量CPUバイナリ)
$SharevoxDockerfile = if ($HasGpu) {
@"
FROM ubuntu:20.04 AS downloader
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y curl tar ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/sv
RUN curl -sL "https://github.com/SHAREVOX/sharevox/releases/download/0.2.1/sharevox-linux-cpu-0.2.1.tar.gz" | tar -xzf - --strip-components=1

FROM sharevox/sharevox_engine:nvidia-ubuntu20.04-latest
USER root
RUN mkdir -p /opt/sharevox_engine/library_info
COPY --from=downloader /tmp/sv/model /opt/sharevox_engine/model
COPY --from=downloader /tmp/sv/speaker_info /opt/sharevox_engine/speaker_info
EXPOSE 50025
ENTRYPOINT ["/entrypoint.sh"]
CMD ["gosu", "user", "/opt/python/bin/python3", "./run.py", "--use_gpu", "--voicelib_dir", "/opt/sharevox_core/", "--runtime_dir", "/opt/onnxruntime/lib", "--host", "0.0.0.0", "--port", "50025"]
"@ -replace "`r`n", "`n"
} else {
@"
FROM ubuntu:20.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y curl tar ca-certificates libgomp1 libsndfile1 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/sharevox
RUN curl -sL "https://github.com/SHAREVOX/sharevox/releases/download/0.2.1/sharevox-linux-cpu-0.2.1.tar.gz" | tar -xzf - --strip-components=1 && chmod +x /opt/sharevox/run
EXPOSE 50025
ENTRYPOINT ["/opt/sharevox/run"]
CMD ["--host", "0.0.0.0", "--port", "50025", "--cpu_num_threads", "$ThreadNum"]
"@ -replace "`r`n", "`n"
}
[System.IO.File]::WriteAllText("$WorkDir\docker\sharevox\Dockerfile", $SharevoxDockerfile, $Utf8NoBom)

# COEIROINK Dockerfile & entrypoint.sh (必ず LF 改行にする)
$CoeiroinkDockerfile = @"
FROM $CoeiroinkBaseImage
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y curl unzip ca-certificates libgomp1 libsndfile1 socat && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/coeiroink
RUN curl -L -o /tmp/coeiroink.zip "$CoeiroinkZipUrl" && \
    unzip -q /tmp/coeiroink.zip -d /opt/coeiroink && \
    rm /tmp/coeiroink.zip && \
    chmod +x /opt/coeiroink/$CoeiroinkDir/engine/engine
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 50031 50032
ENTRYPOINT ["/entrypoint.sh"]
"@ -replace "`r`n", "`n"
[System.IO.File]::WriteAllText("$WorkDir\docker\coeiroink\Dockerfile", $CoeiroinkDockerfile, $Utf8NoBom)

$CoeiroinkEntrypoint = @"
#!/bin/bash
set -e
DIR=`$(ls -d /opt/coeiroink/COEIROINK_LINUX_* | head -n 1)
cd "`$DIR/engine"
export LD_LIBRARY_PATH="`$DIR/engine:`$DIR/engine/torch/lib:/usr/local/cuda/lib64:`$LD_LIBRARY_PATH"
socat TCP-LISTEN:50031,fork,reuseaddr TCP:127.0.0.1:50032 &
exec "`$DIR/engine/engine"
"@ -replace "`r`n", "`n"
[System.IO.File]::WriteAllText("$WorkDir\docker\coeiroink\entrypoint.sh", $CoeiroinkEntrypoint, $Utf8NoBom)

# docker-compose.worker.yml (VOICEVOX / SHAREVOX / COEIROINK)
$VvDeploy = if ($HasGpu) {
@"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
        limits:
          memory: ${RamVv}G
"@
} else {
@"
    deploy:
      resources:
        limits:
          cpus: "${AllocCpus}.0"
          memory: ${RamVv}G
"@
}

$SvDeploy = if ($HasGpu) {
@"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
        limits:
          memory: ${RamSv}G
"@
} else {
@"
    deploy:
      resources:
        limits:
          cpus: "${AllocCpus}.0"
          memory: ${RamSv}G
"@
}

$CiDeploy = if ($HasGpu) {
@"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
        limits:
          memory: ${RamCi}G
"@
} else {
@"
    deploy:
      resources:
        limits:
          cpus: "${AllocCpus}.0"
          memory: ${RamCi}G
"@
}

$ComposeContent = @"
# ==============================================================================
# TTSBot Compute Worker (Windows) - 3大TTSエンジン定義
# VOICEVOX / SHAREVOX / COEIROINK
# ==============================================================================

name: ttsbot-worker-engines

services:
  worker-voicevox:
    image: $VoicevoxImage
    container_name: ttsworker-voicevox
    restart: unless-stopped
    ports:
      - "50021:50021"
    environment:
      - OMP_NUM_THREADS=$ThreadNum
      - MKL_NUM_THREADS=$ThreadNum
$VvDeploy
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "2"

  worker-sharevox:
    build:
      context: ./docker/sharevox
      dockerfile: Dockerfile
    container_name: ttsworker-sharevox
    restart: unless-stopped
    ports:
      - "50025:50025"
    environment:
      - OMP_NUM_THREADS=$ThreadNum
      - MKL_NUM_THREADS=$ThreadNum
      - SV_CPU_NUM_THREADS=$ThreadNum
$SvDeploy
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "2"

  worker-coeiroink:
    build:
      context: ./docker/coeiroink
      dockerfile: Dockerfile
    container_name: ttsworker-coeiroink
    restart: unless-stopped
    ports:
      - "50031:50031"
    environment:
      - OMP_NUM_THREADS=$ThreadNum
      - MKL_NUM_THREADS=$ThreadNum
      - TORCH_NUM_THREADS=$ThreadNum
      - OPENBLAS_NUM_THREADS=$ThreadNum
    volumes:
      - "$WorkDirUnix/coeiroink_models:/opt/coeiroink/$CoeiroinkDir/speaker_info"
$CiDeploy
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "2"
"@ -replace "`r`n", "`n"
[System.IO.File]::WriteAllText("$WorkDir\docker-compose.worker.yml", $ComposeContent, $Utf8NoBom)

# docker-compose.agent.yml (Worker Agent 常駐プロセス)
$Hostname = $env:COMPUTERNAME
$AgentDeploy = if ($HasGpu) {
@"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
        limits:
          cpus: "0.5"
"@
} else {
@"
    deploy:
      resources:
        limits:
          cpus: "0.5"
"@
}

$AgentCompose = @"
# ==============================================================================
# TTSBot Worker Agent (Windows) - 常駐管理プロセス
# ==============================================================================

name: ttsbot-worker-agent

services:
  worker-agent:
    image: python:3.14-slim
    container_name: ttsworker-agent
    restart: unless-stopped
    ports:
      - "50030:50030"
    volumes:
      - //var/run/docker.sock:/var/run/docker.sock
      - ./:/work
      - ./docker/agent:/app
      - ./coeiroink_models:/coeiroink_models
      - ./docker-compose.worker.yml:/app/docker-compose.worker.yml
    working_dir: /app
    environment:
      - TZ=Asia/Tokyo
      - MASTER_URL=$MasterUrl
      - WORKER_IP=$LocalIp
      - HAS_GPU=$($HasGpu.ToString().ToLower())
      - GPU_NAME=$GpuName
      - GPU_VRAM_MB=$GpuVramMb
      - AGENT_PORT=50030
      - RSYNC_PORT=8730
      - IDLE_TIMEOUT_SECONDS=900
      - WORK_DIR=/work
      - WORKER_COMPOSE_FILE=/work/docker-compose.worker.yml
      - NODE_ID=worker_${Hostname}
      - HOST_HOSTNAME=${Hostname}
    command: >
      sh -c "pip install --quiet --no-cache-dir --root-user-action=ignore aiohttp && python3 agent.py"
$AgentDeploy
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
"@ -replace "`r`n", "`n"
[System.IO.File]::WriteAllText("$WorkDir\docker-compose.agent.yml", $AgentCompose, $Utf8NoBom)

# Windows Defender ファイアウォール規則の追加 (LAN内通信を許可)
try {
    $RulePorts = @(50021, 50025, 50031, 50030)
    foreach ($p in $RulePorts) {
        $ruleName = "TTSBot-Worker-Port-$p"
        if (-not (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -Name $ruleName -DisplayName "TTSBot Worker ($p)" -Direction Inbound -LocalPort $p -Protocol TCP -Action Allow -Profile Any -ErrorAction SilentlyContinue | Out-Null
        }
    }
} catch { }

# Agent スクリプト取得
Write-Host "  📥 Worker Agent スクリプトを取得中..." -ForegroundColor Blue
try {
    Invoke-WebRequest -Uri "$MasterUrl/static/agent.py" -OutFile "$WorkDir\docker\agent\agent.py" -UseBasicParsing
    Write-Host "  ✅ agent.py を取得しました。" -ForegroundColor Green
} catch {
    Write-Host "  ⚠️  agent.py の取得に失敗しました。手動で配置してください。" -ForegroundColor Yellow
}

Write-Host "✅ 構成ファイルの展開が完了しました。" -ForegroundColor Green

# 3.5. COEIROINK モデルの同期
Write-Host "`n📥 [3.5/5] Master サーバーから COEIROINK 話者モデルを同期中..." -ForegroundColor Cyan

$ModelsDir = "$WorkDir\coeiroink_models"
if (-not (Test-Path $ModelsDir)) {
    New-Item -ItemType Directory -Path $ModelsDir -Force | Out-Null
}

$ModelsListUrl = "$MasterUrl/api/worker/models/list"
try {
    # Invoke-RestMethod で Master から JSON を直接取得 (PowerShell ネイティブ)
    $ModelsData = Invoke-RestMethod -Uri $ModelsListUrl -TimeoutSec 15 -UseBasicParsing
    if ($ModelsData -and $ModelsData.models) {
        $ModelList = $ModelsData.models
        $TotalModels = $ModelList.Count
        Write-Host "  Master に登録されている COEIROINK モデル: $TotalModels 件" -ForegroundColor Green

        $TarExe = (Get-Command tar.exe -ErrorAction SilentlyContinue)?.Source
        $CurlExe = (Get-Command curl.exe -ErrorAction SilentlyContinue)?.Source

        $CurrentIdx = 0
        foreach ($m in $ModelList) {
            $CurrentIdx++
            $fName = $m.folder_name
            $spkName = $m.speaker_name
            $sizeMb = $m.size_mb
            $targetFolder = "$ModelsDir\$fName"

            # 同期済みかどうかの厳密なチェック (metas.json, model配下の実モデルファイル, サイズ照合)
            $isSynced = $false
            if ((Test-Path "$targetFolder\metas.json") -and (Test-Path "$targetFolder\model")) {
                $modelFiles = Get-ChildItem -Path "$targetFolder\model" -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.Extension -in ".pth", ".onnx" }
                if ($modelFiles -and $modelFiles.Count -ge 1) {
                    $isSynced = $true
                    $expBytes = if ($m.size_bytes) { [int64]$m.size_bytes } else { 0 }
                    if ($expBytes -gt 0) {
                        $localBytes = (Get-ChildItem -Path $targetFolder -Recurse -File -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
                        if ($localBytes -lt ($expBytes * 0.90)) {
                            $isSynced = $false
                        }
                    }
                }
            }

            if ($isSynced) {
                Write-Host "  [$CurrentIdx/$TotalModels] ✨ $spkName (${sizeMb}MB) は同期済みです (スキップ)" -ForegroundColor DarkGray
                continue
            }

            # 過去の中途半端な展開失敗の残骸があればクリーンアップして再試行可能にする
            if (Test-Path $targetFolder) {
                Remove-Item -Path $targetFolder -Recurse -Force -ErrorAction SilentlyContinue
            }

            Write-Host "  [$CurrentIdx/$TotalModels] $spkName (${sizeMb}MB) をダウンロード・展開中..." -ForegroundColor Yellow
            $DownloadUrl = "$MasterUrl/api/worker/models/download/$fName"
            $TempTar = "$ModelsDir\_temp_$fName.tar"

            # バイナリ破損防止のため、ファイルに直接保存してから tar 展開
            try {
                if ($CurlExe) {
                    & $CurlExe -sSL "$DownloadUrl" -o "$TempTar"
                } else {
                    Invoke-WebRequest -Uri "$DownloadUrl" -OutFile "$TempTar" -UseBasicParsing
                }

                if (Test-Path "$TempTar") {
                    if ($TarExe) {
                        & $TarExe -xf "$TempTar" -C "$ModelsDir"
                    } else {
                        tar -xf "$TempTar" -C "$ModelsDir"
                    }
                    Remove-Item -Force "$TempTar" -ErrorAction SilentlyContinue
                }

                $isSuccess = $false
                if ((Test-Path "$targetFolder\metas.json") -and (Test-Path "$targetFolder\model")) {
                    $modelFiles = Get-ChildItem -Path "$targetFolder\model" -Recurse -File -ErrorAction SilentlyContinue
                    if ($modelFiles -and $modelFiles.Count -ge 1) {
                        $isSuccess = $true
                    }
                }

                if ($isSuccess) {
                    Write-Host "  [$CurrentIdx/$TotalModels] ✅ $spkName の配置完了" -ForegroundColor Green
                } else {
                    Write-Host "  [$CurrentIdx/$TotalModels] ⚠️ $spkName の展開に失敗しました" -ForegroundColor Red
                    if (Test-Path $targetFolder) {
                        Remove-Item -Path $targetFolder -Recurse -Force -ErrorAction SilentlyContinue
                    }
                }
            } catch {
                Write-Host "  [$CurrentIdx/$TotalModels] ⚠️ ダウンロードエラー: $_" -ForegroundColor Red
                if (Test-Path "$TempTar") { Remove-Item -Force "$TempTar" -ErrorAction SilentlyContinue }
                if (Test-Path $targetFolder) { Remove-Item -Path $targetFolder -Recurse -Force -ErrorAction SilentlyContinue }
            }
        }
    } else {
        Write-Host "  ⚠️ Master からモデル一覧を取得できませんでした。スキップします。" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  ⚠️ モデル同期中にエラーが発生しました: $_" -ForegroundColor Yellow
}

# 4. TTS エンジン起動
Write-Host "`n🚀 [4/5] 3大TTSエンジン (VOICEVOX / SHAREVOX / COEIROINK) をビルド・起動中..." -ForegroundColor Blue
Write-Host "  ※ 既存のコンテナがある場合は最新構成で自動更新・再作成されます。" -ForegroundColor Yellow
if ($HasGpu) {
    Write-Host "  ※ GPU モードのため COEIROINK GPU 版 (約3.5GB) のダウンロードと初回ビルドが行われます。数分程度お待ちください。" -ForegroundColor Cyan
}

# 旧形式コンテナ (-win 付き) が残っていればポート競合防止のため削除
$OldContainers = @("ttsworker-voicevox-win", "ttsworker-sharevox-win", "ttsworker-coeiroink-win", "ttsworker-agent-win")
foreach ($c in $OldContainers) {
    try {
        $exists = docker ps -a --filter "name=^/${c}$" --format "{{.Names}}" 2>$null
        if ($exists) {
            Write-Host "  🧹 移行のため旧コンテナ ($c) を削除中..." -ForegroundColor Yellow
            docker rm -f $c 2>$null | Out-Null
        }
    } catch { }
}

docker compose -f "$WorkDir\docker-compose.worker.yml" up -d --build --remove-orphans

# 5. Worker Agent 起動 (最新環境変数・GPU設定を反映して再作成)
Write-Host "`n🤖 [5/5] Worker Agent (自律管理プロセス) を起動中..." -ForegroundColor Blue
docker compose -f "$WorkDir\docker-compose.agent.yml" up -d --force-recreate

# 各エンジン ヘルスチェック待機
Write-Host "`n⏳ 各エンジンの起動を待機中..." -ForegroundColor Blue

# VOICEVOX
$VvReady = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:50021/version" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $VvReady = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
}
if ($VvReady) {
    Write-Host "  ✅ VOICEVOX (50021): 正常起動！" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  VOICEVOX: 待機タイムアウト (バックグラウンドで起動中)" -ForegroundColor Yellow
}

# SHAREVOX
$SvReady = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:50025/version" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $SvReady = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
}
if ($SvReady) {
    Write-Host "  ✅ SHAREVOX (50025): 正常起動！" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  SHAREVOX: 待機タイムアウト (バックグラウンドで起動中)" -ForegroundColor Yellow
}

# COEIROINK (GPU版のロード時間を考慮して最大60秒待機)
$CiReady = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:50031/docs" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200 -or $r.StatusCode -eq 404) { $CiReady = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
}
if ($CiReady) {
    Write-Host "  ✅ COEIROINK (50031): 正常起動！ $(if ($HasGpu) { '(GPU)' } else { '(CPU)' })" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  COEIROINK: 待機タイムアウト (バックグラウンドで起動中)" -ForegroundColor Yellow
}

# Agent ヘルスチェック
$AgentReady = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:50030/health" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $AgentReady = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
}
if ($AgentReady) {
    Write-Host "  ✅ Worker Agent (50030): 正常起動！" -ForegroundColor Green
} else {
    Write-Host "  ⚠️  Worker Agent: 待機タイムアウト。確認コマンド: docker logs ttsworker-agent" -ForegroundColor Yellow
}

# Master への自動登録確認
$RegisteredOk = $false
if ($AgentReady) {
    Write-Host "`n⏳ Master ($MasterUrl) への自動登録を確認中..." -ForegroundColor Blue
    for ($i = 0; $i -lt 15; $i++) {
        try {
            $statusResp = Invoke-RestMethod -Uri "http://127.0.0.1:50030/status" -UseBasicParsing -TimeoutSec 2
            if ($statusResp.master_registered -eq $true) {
                $RegisteredOk = $true
                break
            }
        } catch { }
        Start-Sleep -Seconds 1
    }
}

# 完了案内
Write-Host ""
Write-Host "==============================================================================" -ForegroundColor Green
Write-Host "   🎉 セットアップ完了！" -ForegroundColor Green
Write-Host "==============================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "このワーカーの情報:" -ForegroundColor White
Write-Host "  LAN IP アドレス : $LocalIp" -ForegroundColor Cyan
Write-Host "  Worker Agent  : http://${LocalIp}:50030" -ForegroundColor Cyan
Write-Host "  VOICEVOX      : http://${LocalIp}:50021 $(if ($HasGpu) { '(GPU)' } else { '(CPU)' })" -ForegroundColor Cyan
Write-Host "  SHAREVOX      : http://${LocalIp}:50025 $(if ($HasGpu) { '(GPU)' } else { '(CPU)' })" -ForegroundColor Cyan
Write-Host "  COEIROINK     : http://${LocalIp}:50031 $(if ($HasGpu) { '(GPU)' } else { '(CPU)' })" -ForegroundColor Cyan
Write-Host ""

if ($RegisteredOk) {
    Write-Host "🎉 Master ($MasterUrl) への自動登録が完了しました！" -ForegroundColor Green
    Write-Host "   WebUI の「ワーカー管理」画面に本ノードが即座に反映されています。" -ForegroundColor Green
    Write-Host "   config.yaml への手動追加は一切不要です！" -ForegroundColor Green
} else {
    Write-Host "ℹ️  Worker Agent が Master ($MasterUrl) へ自動登録を継続実行しています。" -ForegroundColor Yellow
    Write-Host "   WebUI の「ワーカー管理」画面で接続状況をご確認ください。" -ForegroundColor Blue
}

Write-Host ""
Write-Host "※参考: オフライン等で手動設定する場合のみ config.yaml に追加する内容:" -ForegroundColor DarkGray
Write-Host @"
  voicevox:
    nodes:
      - id: "win_worker_$($LocalIp -replace '\.', '_')"
        url: "http://${LocalIp}:50021"
        priority: $Priority
        max_concurrent: $MaxConcurrent
        enabled: true
"@ -ForegroundColor DarkGray
Write-Host ""
Write-Host "アップデート方法 (自動): WebUI の Worker 管理ページから「アップデート」ボタン" -ForegroundColor White
Write-Host "または: curl -X POST http://${LocalIp}:50030/update" -ForegroundColor Yellow
Write-Host ""
Write-Host "完全自動化されました。セットアップはこれで終了です！ 🎉" -ForegroundColor Green
