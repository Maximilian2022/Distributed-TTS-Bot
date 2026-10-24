# ==============================================================================
# TTSBot Volunteer Worker GUI 設定ランチャー (Windows ネイティブ GUI)
# ==============================================================================
# 外部依存ゼロ: PowerShell 標準の .NET Windows Forms を使用
# 特徴:
#   - 実行環境の選択 (Windows ネイティブ直接実行 vs Docker Desktop コンテナ)
#   - 共有トークン必須化 & 認証保護 (Cloudflare Tunnel / インターネット経由)
#   - VOICEVOX / SHAREVOX / COEIROINK の自動検出 & 自動セットアップ支援
#   - ネイティブエンジンの最小化・バックグラウンド起動 (ゲーム画面の邪魔にならない)
#   - ローカルエンジンの最新バージョン自動チェック & アップデート案内 (ユーザー承認)
#   - デスクトップ & スタートアップへのワンクリックショートカット配置
#   - ワンライナー (irm ... | iex) 完全対応 (自己完結エージェント自動展開)
# ==============================================================================

param(
    [string]$DefaultServerUrl = "__DEFAULT_SERVER_WS_URL__",
    [string]$DefaultToken = "__DEFAULT_WORKER_TOKEN__",
    [switch]$AutoStartEngineOnly
)

if ($DefaultServerUrl -like "*__DEFAULT_*") { $DefaultServerUrl = "" }
if ($DefaultToken -like "*__DEFAULT_*") { $DefaultToken = "" }

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# 実行元ディレクトリの解決 (iex パイプライン実行時は ~/.ttsbot-worker へフォールバック)
$WorkerDir = if ($PSScriptRoot) {
    $PSScriptRoot
} else {
    $defaultDir = Join-Path $env:USERPROFILE ".ttsbot-worker"
    if (-not (Test-Path $defaultDir)) {
        New-Item -ItemType Directory -Path $defaultDir -Force | Out-Null
    }
    $defaultDir
}

$ConfigPath = Join-Path $WorkerDir "config.json"
$Config = @{}

if (Test-Path $ConfigPath) {
    try {
        $Config = Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {}
}

# 1. ローカルエンジンのポート死活検査
function Test-PortOpen([string]$HostName, [int]$Port) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $asyncResult = $client.BeginConnect($HostName, $Port, $null, $null)
        $wait = $asyncResult.AsyncWaitHandle.WaitOne(300, $false)
        if ($wait) {
            $client.EndConnect($asyncResult)
            $client.Close()
            return $true
        }
        $client.Close()
        return $false
    } catch {
        return $false
    }
}

# 2. 各ポートのエンジン種別スマートAPI判定
function Get-EngineInfoAtPort([int]$Port) {
    if (-not (Test-PortOpen "127.0.0.1" $Port)) { return $null }

    # A. engine_manifest 確認 (VOICEVOX / SHAREVOX 公式API)
    try {
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/engine_manifest")
        $req.Timeout = 800
        $resp = $req.GetResponse()
        $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
        $json = $sr.ReadToEnd()
        $resp.Close()
        if ($json -match '(?i)"brand_name"\s*:\s*"SHAREVOX"' -or $json -match '(?i)"name"\s*:\s*"SHAREVOX"') {
            return @{ type = "sharevox"; port = $Port; url = "http://127.0.0.1:$Port"; label = "SHAREVOX (ポート $Port)" }
        }
        if ($json -match '(?i)"brand_name"\s*:\s*"VOICEVOX"' -or $json -match '(?i)"name"\s*:\s*"VOICEVOX"') {
            return @{ type = "voicevox"; port = $Port; url = "http://127.0.0.1:$Port"; label = "VOICEVOX (ポート $Port)" }
        }
    } catch {}

    # B. COEIROINK 判定 (/v1/speakers)
    try {
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/v1/speakers")
        $req.Timeout = 800
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $resp.Close()
        if ($code -eq 200) {
            return @{ type = "coeiroink"; port = $Port; url = "http://127.0.0.1:$Port"; label = "COEIROINK (ポート $Port)" }
        }
    } catch {}

    # C. 話者名判定フォールバック (/speakers)
    try {
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/speakers")
        $req.Timeout = 800
        $resp = $req.GetResponse()
        $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
        $json = $sr.ReadToEnd()
        $resp.Close()
        if ($json -match '小春音アミ' -or $json -match 'SHAREVOX') {
            return @{ type = "sharevox"; port = $Port; url = "http://127.0.0.1:$Port"; label = "SHAREVOX (ポート $Port)" }
        }
        return @{ type = "voicevox"; port = $Port; url = "http://127.0.0.1:$Port"; label = "VOICEVOX (ポート $Port)" }
    } catch {}

    return @{ type = "unknown"; port = $Port; url = "http://127.0.0.1:$Port"; label = "不明なエンジン (ポート $Port)" }
}

function Scan-AllLocalEngines {
    $found = @{}
    $ports = @(50021, 50025, 50031, 50032)
    foreach ($p in $ports) {
        $info = Get-EngineInfoAtPort $p
        if ($info) {
            $t = $info.type
            if (-not $found.ContainsKey($t)) {
                $found[$t] = $info
            }
        }
    }
    return $found
}

# 3. エンジンのアップデート確認 (VOICEVOX)
function Check-VoicevoxUpdate([string]$currentUrl = "http://127.0.0.1:50021") {
    try {
        # 現在のローカルバージョン取得
        $localVer = ""
        try {
            $req = [System.Net.HttpWebRequest]::Create("$currentUrl/version")
            $req.Timeout = 800
            $resp = $req.GetResponse()
            $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
            $localVer = $sr.ReadToEnd().Trim('"').Trim()
            $resp.Close()
        } catch {}

        if (-not $localVer) { return }

        # GitHub Releases の最新タグ取得
        $ghReq = [System.Net.HttpWebRequest]::Create("https://api.github.com/repos/VOICEVOX/voicevox/releases/latest")
        $ghReq.UserAgent = "TTSBot-Volunteer-Worker"
        $ghReq.Timeout = 1200
        $ghResp = $ghReq.GetResponse()
        $ghSr = New-Object System.IO.StreamReader($ghResp.GetResponseStream())
        $ghJson = $ghSr.ReadToEnd() | ConvertFrom-Json
        $ghResp.Close()

        $latestVer = $ghJson.tag_name.TrimStart('v').Trim()

        if ($latestVer -and $localVer -and ($latestVer -ne $localVer)) {
            $msg = "VOICEVOX の新しいバージョンが利用可能です！`n`n現在利用中: v$localVer`n最新バージョン: v$latestVer`n`n公式配布サイトを開いてアップデートしますか？"
            $ans = [System.Windows.Forms.MessageBox]::Show(
                $msg,
                "VOICEVOX アップデートの確認",
                [System.Windows.Forms.MessageBoxButtons]::YesNo,
                [System.Windows.Forms.MessageBoxIcon]::Information
            )
            if ($ans -eq [System.Windows.Forms.DialogResult]::Yes) {
                Start-Process "https://voicevox.hiroshiba.jp/"
            }
        }
    } catch {}
}

# 4. エンジンのインストール判定 & 起動・導入支援
function Get-EngineInstallInfo {
    $info = @{
        voicevox  = $null
        sharevox  = $null
        coeiroink = $null
    }

    # VOICEVOX 探索
    $vvPaths = @(
        (Join-Path $env:LOCALAPPDATA "Programs\VOICEVOX\VOICEVOX.exe"),
        "C:\Program Files\VOICEVOX\VOICEVOX.exe",
        "C:\Program Files (x86)\VOICEVOX\VOICEVOX.exe"
    )
    foreach ($p in $vvPaths) {
        if (Test-Path $p) { $info["voicevox"] = $p; break }
    }

    # SHAREVOX 探索
    $svPaths = @(
        (Join-Path $env:LOCALAPPDATA "Programs\SHAREVOX\SHAREVOX.exe"),
        "C:\Program Files\SHAREVOX\SHAREVOX.exe",
        "C:\Program Files (x86)\SHAREVOX\SHAREVOX.exe"
    )
    foreach ($p in $svPaths) {
        if (Test-Path $p) { $info["sharevox"] = $p; break }
    }

    # COEIROINK 探索 (ポータブル / 各種配置先)
    $ciPaths = @(
        (Join-Path $env:LOCALAPPDATA "Programs\COEIROINK\COEIROINKonv2.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\COEIROINK\COEIROINK.exe"),
        (Join-Path $env:USERPROFILE "COEIROINK\COEIROINKonv2.exe"),
        (Join-Path $env:USERPROFILE "COEIROINK\COEIROINK.exe"),
        "C:\COEIROINK\COEIROINKonv2.exe",
        "C:\COEIROINK\COEIROINK.exe"
    )
    foreach ($p in $ciPaths) {
        if (Test-Path $p) { $info["coeiroink"] = $p; break }
    }

    return $info
}

function Start-NativeVoicevox([System.Windows.Forms.Label]$statusLabel = $null) {
    if (Test-PortOpen "127.0.0.1" 50021) {
        if ($statusLabel) { $statusLabel.Text = "[OK] VOICEVOX は既に起動しています (50021)" }
        return $true
    }
    $info = Get-EngineInstallInfo
    if ($info["voicevox"] -and (Test-Path $info["voicevox"])) {
        if ($statusLabel) { $statusLabel.Text = "[起動中] VOICEVOX を最小化起動中..." }
        Start-Process -FilePath $info["voicevox"] -WindowStyle Minimized
        for ($i = 0; $i -lt 15; $i++) {
            Start-Sleep -Seconds 1
            if (Test-PortOpen "127.0.0.1" 50021) {
                if ($statusLabel) { $statusLabel.Text = "[OK] VOICEVOX の起動を確認しました！" }
                return $true
            }
        }
    }
    return $false
}

function Start-NativeSharevox([System.Windows.Forms.Label]$statusLabel = $null) {
    if (Test-PortOpen "127.0.0.1" 50025 -or Test-PortOpen "127.0.0.1" 50021) {
        return $true
    }
    $info = Get-EngineInstallInfo
    if ($info["sharevox"] -and (Test-Path $info["sharevox"])) {
        if ($statusLabel) { $statusLabel.Text = "[起動中] SHAREVOX を最小化起動中..." }
        Start-Process -FilePath $info["sharevox"] -WindowStyle Minimized
        for ($i = 0; $i -lt 15; $i++) {
            Start-Sleep -Seconds 1
            if (Test-PortOpen "127.0.0.1" 50025 -or Test-PortOpen "127.0.0.1" 50021) {
                return $true
            }
        }
    }
    return $false
}

function Start-NativeCoeiroink([System.Windows.Forms.Label]$statusLabel = $null) {
    if (Test-PortOpen "127.0.0.1" 50031 -or Test-PortOpen "127.0.0.1" 50032) {
        return $true
    }
    $info = Get-EngineInstallInfo
    if ($info["coeiroink"] -and (Test-Path $info["coeiroink"])) {
        if ($statusLabel) { $statusLabel.Text = "[起動中] COEIROINK を最小化起動中..." }
        Start-Process -FilePath $info["coeiroink"] -WindowStyle Minimized
        for ($i = 0; $i -lt 15; $i++) {
            Start-Sleep -Seconds 1
            if (Test-PortOpen "127.0.0.1" 50031 -or Test-PortOpen "127.0.0.1" 50032) {
                return $true
            }
        }
    }
    return $false
}

function Get-GitHubLatestReleaseTag([string]$repo, [string]$fallbackTag) {
    try {
        $req = [System.Net.HttpWebRequest]::Create("https://github.com/$repo/releases/latest")
        $req.AllowAutoRedirect = $false
        $req.Timeout = 5000
        $req.UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        $resp = $req.GetResponse()
        $loc = $resp.GetResponseHeader("Location")
        $resp.Close()
        if ($loc -match "/tag/([^/]+)$") {
            return $Matches[1].Trim()
        }
    } catch { }
    return $fallbackTag
}

function Install-EngineFromUrl([string]$engineName, [string]$downloadUrl, [string]$fileName, [System.Windows.Forms.Label]$statusLabel) {
    $tempDir = [System.IO.Path]::GetTempPath()
    $destFile = Join-Path $tempDir $fileName

    if ($statusLabel) { $statusLabel.Text = "[DL中] $engineName インストーラーをダウンロード中..." }
    Write-Host "▶ $engineName インストーラーをダウンロード中: $downloadUrl" -ForegroundColor Cyan
    
    try {
        if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
            & curl.exe -fL "$downloadUrl" -o "$destFile"
        } else {
            $wc = New-Object System.Net.WebClient
            $wc.Headers.Add("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
            $wc.DownloadFile($downloadUrl, $destFile)
        }
        
        if (Test-Path $destFile) {
            if ($statusLabel) { $statusLabel.Text = "[起動] インストーラーを起動しています..." }
            Write-Host "✓ ダウンロード完了: $destFile" -ForegroundColor Green
            Write-Host "▶ インストーラーを起動中 (画面の指示に従って完了してください)..." -ForegroundColor Cyan
            Start-Process -FilePath $destFile
            return $true
        }
    } catch {
        Write-Warning "ダウンロードに失敗しました: $_"
        if ($statusLabel) { $statusLabel.Text = "[エラー] ダウンロード失敗。手動でお試しください" }
    }
    return $false
}

function Show-EngineInstallOptionsDialog([string]$engineName, [string]$websiteUrl, [string]$autoInstallerUrl, [string]$installerFileName, [System.Windows.Forms.Label]$statusLabel, [System.Windows.Forms.Form]$parentForm) {
    $dlg = New-Object System.Windows.Forms.Form
    $dlg.Text = "$engineName のセットアップ"
    $dlg.Size = New-Object System.Drawing.Size(460, 240)
    $dlg.StartPosition = "CenterParent"
    $dlg.FormBorderStyle = "FixedDialog"
    $dlg.MaximizeBox = $false
    $dlg.MinimizeBox = $false
    $dlg.BackColor = [System.Drawing.Color]::FromArgb(47, 49, 54)
    $dlg.ForeColor = [System.Drawing.Color]::White

    $lbl = New-Object System.Windows.Forms.Label
    $lbl.Text = "$engineName がPCにインストールされていません。`nセットアップ方法を選択してください："
    $lbl.Location = New-Object System.Drawing.Point(20, 20)
    $lbl.Size = New-Object System.Drawing.Size(410, 40)
    $lbl.Font = New-Object System.Drawing.Font("Segoe UI", 9.5, [System.Drawing.FontStyle]::Bold)
    $dlg.Controls.Add($lbl)

    $hasAuto = -not [string]::IsNullOrEmpty($autoInstallerUrl)

    # 1. 自動スクリプトインストールボタン
    $btnAuto = New-Object System.Windows.Forms.Button
    $btnAuto.Text = if ($hasAuto) { "自動でダウンロードしてインストール (推奨)" } else { "自動ダウンロード非対応 (公式サイトへ)" }
    $btnAuto.Location = New-Object System.Drawing.Point(20, 75)
    $btnAuto.Size = New-Object System.Drawing.Size(400, 36)
    $btnAuto.BackColor = if ($hasAuto) { [System.Drawing.Color]::FromArgb(88, 101, 242) } else { [System.Drawing.Color]::FromArgb(79, 84, 92) }
    $btnAuto.ForeColor = [System.Drawing.Color]::White
    $btnAuto.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $btnAuto.Font = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)
    $btnAuto.Enabled = $hasAuto
    $btnAuto.Add_Click({
        $dlg.DialogResult = [System.Windows.Forms.DialogResult]::OK
        $dlg.Close()
        Install-EngineFromUrl $engineName $autoInstallerUrl $installerFileName $statusLabel
    })
    $dlg.Controls.Add($btnAuto)

    # 2. 手動インストール (ブラウザを開く) ボタン
    $btnManual = New-Object System.Windows.Forms.Button
    $btnManual.Text = "手動でダウンロード (公式サイトを開く)"
    $btnManual.Location = New-Object System.Drawing.Point(20, 120)
    $btnManual.Size = New-Object System.Drawing.Size(400, 32)
    $btnManual.BackColor = [System.Drawing.Color]::FromArgb(79, 84, 92)
    $btnManual.ForeColor = [System.Drawing.Color]::White
    $btnManual.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $btnManual.Font = New-Object System.Drawing.Font("Segoe UI", 9)
    $btnManual.Add_Click({
        $dlg.DialogResult = [System.Windows.Forms.DialogResult]::Yes
        $dlg.Close()
        Start-Process $websiteUrl
    })
    $dlg.Controls.Add($btnManual)

    # 3. キャンセルボタン
    $btnCancel = New-Object System.Windows.Forms.Button
    $btnCancel.Text = "キャンセル"
    $btnCancel.Location = New-Object System.Drawing.Point(320, 162)
    $btnCancel.Size = New-Object System.Drawing.Size(100, 26)
    $btnCancel.BackColor = [System.Drawing.Color]::FromArgb(64, 68, 75)
    $btnCancel.ForeColor = [System.Drawing.Color]::White
    $btnCancel.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $btnCancel.Font = New-Object System.Drawing.Font("Segoe UI", 8.5)
    $btnCancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $dlg.Controls.Add($btnCancel)

    $dlg.ShowDialog($parentForm) | Out-Null
}

function Start-DockerEngines([bool]$enableVv, [bool]$enableSv, [System.Windows.Forms.Label]$statusLabel = $null) {
    if ($statusLabel) { $statusLabel.Text = "[Docker] Docker Desktop 環境を確認中..." }
    try {
        docker ps 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            if ($statusLabel) { $statusLabel.Text = "[エラー] Docker Desktop が起動していません" }
            return $false
        }
    } catch {
        if ($statusLabel) { $statusLabel.Text = "[エラー] Docker コマンドが見つかりません" }
        return $false
    }

    if ($enableVv -and -not (Test-PortOpen "127.0.0.1" 50021)) {
        if ($statusLabel) { $statusLabel.Text = "[Docker] VOICEVOX コンテナを起動中..." }
        docker run -d --name ttsbot-worker-voicevox --restart unless-stopped -p 50021:50021 voicevox/voicevox_engine:cpu-latest 2>&1 | Out-Null
    }

    if ($enableSv -and -not (Test-PortOpen "127.0.0.1" 50025)) {
        if ($statusLabel) { $statusLabel.Text = "[Docker] SHAREVOX コンテナを起動中 (ポート 50025)..." }
        docker run -d --name ttsbot-worker-sharevox --restart unless-stopped -p 50025:50021 sharevox/sharevox_engine:cpu-latest 2>&1 | Out-Null
    }

    Start-Sleep -Seconds 2
    return $true
}

# バッチ等のバックグラウンドからエンジン起動のみを要求された場合
if ($AutoStartEngineOnly) {
    $started = Start-NativeVoicevox
    exit $(if ($started) { 0 } else { 1 })
}

# --- 初期エンジンスキャン ---
$DetectedEngines = Scan-AllLocalEngines
$HasAnyEngine = $DetectedEngines.Count -gt 0

# 5. フォームの定義
$Form = New-Object System.Windows.Forms.Form
$Form.Text = "TTSBot Volunteer Worker 設定ランチャー"
$Form.Size = New-Object System.Drawing.Size(540, 780)
$Form.StartPosition = "CenterScreen"
$Form.FormBorderStyle = "FixedDialog"
$Form.MaximizeBox = $false
$Form.BackColor = [System.Drawing.Color]::FromArgb(32, 34, 37)
$Form.ForeColor = [System.Drawing.Color]::White
$FontTitle = New-Object System.Drawing.Font("Segoe UI", 11, [System.Drawing.FontStyle]::Bold)
$FontRegular = New-Object System.Drawing.Font("Segoe UI", 9)
$FontSmall = New-Object System.Drawing.Font("Segoe UI", 8)
$ColorAccent = [System.Drawing.Color]::FromArgb(88, 101, 242)
$ColorCardBg = [System.Drawing.Color]::FromArgb(47, 49, 54)
$ColorInputBg = [System.Drawing.Color]::FromArgb(64, 68, 75)

# --- タイトルヘッダー ---
$Header = New-Object System.Windows.Forms.Label
$Header.Text = "TTSBot ボランティア参加設定"
$Header.Font = $FontTitle
$Header.Location = New-Object System.Drawing.Point(20, 12)
$Header.Size = New-Object System.Drawing.Size(480, 24)
$Form.Controls.Add($Header)

$SubHeader = New-Object System.Windows.Forms.Label
$SubHeader.Text = "PCの余剰パワーを提供してDiscord読み上げを高速化・冗長化します。"
$SubHeader.Font = $FontSmall
$SubHeader.ForeColor = [System.Drawing.Color]::LightGray
$SubHeader.Location = New-Object System.Drawing.Point(22, 36)
$SubHeader.Size = New-Object System.Drawing.Size(480, 18)
$Form.Controls.Add($SubHeader)

# --- 接続情報グループ ---
$LblServer = New-Object System.Windows.Forms.Label
$LblServer.Text = "接続先サーバー URL (WSS):"
$LblServer.Font = $FontRegular
$LblServer.Location = New-Object System.Drawing.Point(20, 58)
$LblServer.Size = New-Object System.Drawing.Size(200, 18)
$Form.Controls.Add($LblServer)

$TxtServer = New-Object System.Windows.Forms.TextBox
$TxtServer.Location = New-Object System.Drawing.Point(20, 76)
$TxtServer.Size = New-Object System.Drawing.Size(480, 24)
$TxtServer.BackColor = $ColorInputBg
$TxtServer.ForeColor = [System.Drawing.Color]::White
$TxtServer.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$DefaultUrlEnv = if ($env:TTSBOT_SERVER_URL) { $env:TTSBOT_SERVER_URL } else { "" }
$DefaultTokenEnv = if ($env:TTSBOT_WORKER_TOKEN) { $env:TTSBOT_WORKER_TOKEN } else { "" }
$TxtServer.Text = if ($Config.server_url) { $Config.server_url } elseif ($DefaultServerUrl) { $DefaultServerUrl } elseif ($DefaultUrlEnv) { $DefaultUrlEnv } else { "wss://<あなたのTTSBotドメイン>/api/worker/ws" }
$Form.Controls.Add($TxtServer)

$LblToken = New-Object System.Windows.Forms.Label
$LblToken.Text = "共有トークン [必須] (Cloudflare / インターネット経由):"
$LblToken.Font = $FontRegular
$LblToken.ForeColor = [System.Drawing.Color]::FromArgb(255, 180, 50)
$LblToken.Location = New-Object System.Drawing.Point(20, 104)
$LblToken.Size = New-Object System.Drawing.Size(400, 18)
$Form.Controls.Add($LblToken)

$TxtToken = New-Object System.Windows.Forms.TextBox
$TxtToken.Location = New-Object System.Drawing.Point(20, 122)
$TxtToken.Size = New-Object System.Drawing.Size(480, 24)
$TxtToken.BackColor = $ColorInputBg
$TxtToken.ForeColor = [System.Drawing.Color]::White
$TxtToken.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$TxtToken.Text = if ($Config.token) { $Config.token } elseif ($DefaultToken) { $DefaultToken } else { $DefaultTokenEnv }
$Form.Controls.Add($TxtToken)

# --- 0. 実行ランタイム (実行環境) グループ ---
$GrpRuntime = New-Object System.Windows.Forms.GroupBox
$GrpRuntime.Text = "実行環境 (ランタイム方式)"
$GrpRuntime.Font = $FontRegular
$GrpRuntime.ForeColor = [System.Drawing.Color]::White
$GrpRuntime.Location = New-Object System.Drawing.Point(20, 150)
$GrpRuntime.Size = New-Object System.Drawing.Size(480, 68)

$RadNative = New-Object System.Windows.Forms.RadioButton
$RadNative.Text = "Windows ネイティブ (直接実行・推奨) - 最速・最小メモリ"
$RadNative.Location = New-Object System.Drawing.Point(15, 18)
$RadNative.Size = New-Object System.Drawing.Size(440, 20)
$SavedRuntime = if ($Config.runtime) { $Config.runtime } else { "native" }
$RadNative.Checked = ($SavedRuntime -ne "docker")

$RadDocker = New-Object System.Windows.Forms.RadioButton
$RadDocker.Text = "Docker Desktop (コンテナ展開) - 選択したエンジンをコンテナで自動起動"
$RadDocker.Location = New-Object System.Drawing.Point(15, 40)
$RadDocker.Size = New-Object System.Drawing.Size(440, 20)
$RadDocker.Checked = -not $RadNative.Checked

$RadNative.Add_CheckedChanged({ Update-EngineCheckboxTexts })
$RadDocker.Add_CheckedChanged({ Update-EngineCheckboxTexts })

$GrpRuntime.Controls.Add($RadNative)
$GrpRuntime.Controls.Add($RadDocker)
$Form.Controls.Add($GrpRuntime)

# --- 1. 提供範囲 (スコープ) グループ ---
$GrpScope = New-Object System.Windows.Forms.GroupBox
$GrpScope.Text = "1. 提供範囲 (スコープ)"
$GrpScope.Font = $FontRegular
$GrpScope.ForeColor = [System.Drawing.Color]::White
$GrpScope.Location = New-Object System.Drawing.Point(20, 222)
$GrpScope.Size = New-Object System.Drawing.Size(480, 94)

$RadGlobal = New-Object System.Windows.Forms.RadioButton
$RadGlobal.Text = "全体共有プールに参加 (全サーバーの読み上げを支援・推奨)"
$RadGlobal.Location = New-Object System.Drawing.Point(15, 18)
$RadGlobal.Size = New-Object System.Drawing.Size(440, 20)
$RadGlobal.Checked = if ($Config.exclusive_guild_id) { $false } else { $true }

$RadDedicated = New-Object System.Windows.Forms.RadioButton
$RadDedicated.Text = "自サーバー専用枠 (自分が参加するサーバーの読み上げのみ処理)"
$RadDedicated.Location = New-Object System.Drawing.Point(15, 38)
$RadDedicated.Size = New-Object System.Drawing.Size(440, 20)
$RadDedicated.Checked = -not $RadGlobal.Checked

$LblGuild = New-Object System.Windows.Forms.Label
$LblGuild.Text = "専用サーバーID:"
$LblGuild.Font = $FontSmall
$LblGuild.Location = New-Object System.Drawing.Point(35, 64)
$LblGuild.Size = New-Object System.Drawing.Size(100, 18)

$TxtGuild = New-Object System.Windows.Forms.TextBox
$TxtGuild.Location = New-Object System.Drawing.Point(140, 62)
$TxtGuild.Size = New-Object System.Drawing.Size(310, 22)
$TxtGuild.BackColor = $ColorInputBg
$TxtGuild.ForeColor = [System.Drawing.Color]::White
$TxtGuild.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$TxtGuild.Text = if ($Config.exclusive_guild_id) { $Config.exclusive_guild_id } else { "" }
$TxtGuild.Enabled = $RadDedicated.Checked

$RadDedicated.Add_CheckedChanged({
    $TxtGuild.Enabled = $RadDedicated.Checked
    if ($RadDedicated.Checked -and -not $TxtGuild.Text) { $TxtGuild.Focus() }
})

$GrpScope.Controls.Add($RadGlobal)
$GrpScope.Controls.Add($RadDedicated)
$GrpScope.Controls.Add($LblGuild)
$GrpScope.Controls.Add($TxtGuild)
$Form.Controls.Add($GrpScope)

# --- 2. 提供エンジン グループ ---
$GrpEngines = New-Object System.Windows.Forms.GroupBox
$GrpEngines.Text = "2. 提供する音声合成エンジン (自動検出 & セットアップ支援)"
$GrpEngines.Font = $FontRegular
$GrpEngines.ForeColor = [System.Drawing.Color]::White
$GrpEngines.Location = New-Object System.Drawing.Point(20, 320)
$GrpEngines.Size = New-Object System.Drawing.Size(480, 160)

function Update-EngineCheckboxTexts {
    $installInfo = Get-EngineInstallInfo
    $isDocker = $RadDocker.Checked

    # VOICEVOX
    $vvRunning = $DetectedEngines["voicevox"]
    $vvInstalled = -not [string]::IsNullOrEmpty($installInfo["voicevox"])
    if ($isDocker) {
        $ChkVv.Text = "VOICEVOX (四国めたん等) " + $(if($vvRunning){"[稼働中]"}else{"[未起動]"})
        $BtnSetupVv.Text = if ($vvRunning) { "稼働中" } else { "コンテナ起動" }
        $BtnSetupVv.BackColor = if ($vvRunning) { [System.Drawing.Color]::FromArgb(59, 165, 93) } else { [System.Drawing.Color]::FromArgb(88, 101, 242) }
        $BtnSetupVv.Enabled = -not $vvRunning
    } else {
        if ($vvRunning) {
            $ChkVv.Text = "VOICEVOX (四国めたん等) [稼働中]"
            $BtnSetupVv.Text = "稼働中"
            $BtnSetupVv.BackColor = [System.Drawing.Color]::FromArgb(59, 165, 93)
            $BtnSetupVv.Enabled = $false
        } elseif ($vvInstalled) {
            $ChkVv.Text = "VOICEVOX (四国めたん等) [導入済]"
            $BtnSetupVv.Text = "起動する"
            $BtnSetupVv.BackColor = [System.Drawing.Color]::FromArgb(88, 101, 242)
            $BtnSetupVv.Enabled = $true
        } else {
            $ChkVv.Text = "VOICEVOX (四国めたん等) [未導入]"
            $BtnSetupVv.Text = "セットアップ"
            $BtnSetupVv.BackColor = [System.Drawing.Color]::FromArgb(230, 126, 34)
            $BtnSetupVv.Enabled = $true
        }
    }

    # COEIROINK
    $ciRunning = $DetectedEngines["coeiroink"]
    $ciInstalled = -not [string]::IsNullOrEmpty($installInfo["coeiroink"])
    if ($ciRunning) {
        $ChkCi.Text = "COEIROINK (つくよみちゃん等) [稼働中]"
        $BtnSetupCi.Text = "稼働中"
        $BtnSetupCi.BackColor = [System.Drawing.Color]::FromArgb(59, 165, 93)
        $BtnSetupCi.Enabled = $false
    } elseif ($ciInstalled) {
        $ChkCi.Text = "COEIROINK (つくよみちゃん等) [導入済]"
        $BtnSetupCi.Text = "起動する"
        $BtnSetupCi.BackColor = [System.Drawing.Color]::FromArgb(88, 101, 242)
        $BtnSetupCi.Enabled = $true
    } else {
        $ChkCi.Text = "COEIROINK (つくよみちゃん等) [未導入]"
        $BtnSetupCi.Text = "セットアップ"
        $BtnSetupCi.BackColor = [System.Drawing.Color]::FromArgb(230, 126, 34)
        $BtnSetupCi.Enabled = $true
    }

    # SHAREVOX
    $svRunning = $DetectedEngines["sharevox"]
    $svInstalled = -not [string]::IsNullOrEmpty($installInfo["sharevox"])
    if ($isDocker) {
        $ChkSv.Text = "SHAREVOX (小春音アミ等) " + $(if($svRunning){"[稼働中]"}else{"[未起動]"})
        $BtnSetupSv.Text = if ($svRunning) { "稼働中" } else { "コンテナ起動" }
        $BtnSetupSv.BackColor = if ($svRunning) { [System.Drawing.Color]::FromArgb(59, 165, 93) } else { [System.Drawing.Color]::FromArgb(88, 101, 242) }
        $BtnSetupSv.Enabled = -not $svRunning
    } else {
        if ($svRunning) {
            $ChkSv.Text = "SHAREVOX (小春音アミ等) [稼働中]"
            $BtnSetupSv.Text = "稼働中"
            $BtnSetupSv.BackColor = [System.Drawing.Color]::FromArgb(59, 165, 93)
            $BtnSetupSv.Enabled = $false
        } elseif ($svInstalled) {
            $ChkSv.Text = "SHAREVOX (小春音アミ等) [導入済]"
            $BtnSetupSv.Text = "起動する"
            $BtnSetupSv.BackColor = [System.Drawing.Color]::FromArgb(88, 101, 242)
            $BtnSetupSv.Enabled = $true
        } else {
            $ChkSv.Text = "SHAREVOX (小春音アミ等) [未導入]"
            $BtnSetupSv.Text = "セットアップ"
            $BtnSetupSv.BackColor = [System.Drawing.Color]::FromArgb(230, 126, 34)
            $BtnSetupSv.Enabled = $true
        }
    }
}

$ChkVv = New-Object System.Windows.Forms.CheckBox
$ChkVv.Location = New-Object System.Drawing.Point(15, 20)
$ChkVv.Size = New-Object System.Drawing.Size(345, 22)
$ChkVv.Checked = if ($Config.engines) { $Config.engines -contains "voicevox" } else { $true }

$BtnSetupVv = New-Object System.Windows.Forms.Button
$BtnSetupVv.Font = $FontSmall
$BtnSetupVv.Location = New-Object System.Drawing.Point(370, 18)
$BtnSetupVv.Size = New-Object System.Drawing.Size(95, 24)
$BtnSetupVv.ForeColor = [System.Drawing.Color]::White
$BtnSetupVv.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
$BtnSetupVv.Add_Click({
    if ($RadDocker.Checked) {
        Start-DockerEngines $true $false $LblEngineStatus
        Start-Sleep -Seconds 1
        $script:DetectedEngines = Scan-AllLocalEngines
        Update-EngineCheckboxTexts
    } else {
        $info = Get-EngineInstallInfo
        if ($info["voicevox"]) {
            Start-NativeVoicevox $LblEngineStatus
            Start-Sleep -Seconds 1
            $script:DetectedEngines = Scan-AllLocalEngines
            Update-EngineCheckboxTexts
        } else {
            $vvTag = Get-GitHubLatestReleaseTag "VOICEVOX/voicevox" "0.26.1"
            $vvUrl = "https://github.com/VOICEVOX/voicevox/releases/download/$vvTag/VOICEVOX.Web.Setup.$vvTag.exe"
            Show-EngineInstallOptionsDialog "VOICEVOX" "https://voicevox.hiroshiba.jp/" $vvUrl "VOICEVOX.Web.Setup.exe" $LblEngineStatus $Form
            Start-Sleep -Seconds 1
            $script:DetectedEngines = Scan-AllLocalEngines
            Update-EngineCheckboxTexts
        }
    }
})

$ChkCi = New-Object System.Windows.Forms.CheckBox
$ChkCi.Location = New-Object System.Drawing.Point(15, 46)
$ChkCi.Size = New-Object System.Drawing.Size(345, 22)
$ChkCi.Checked = if ($Config.engines) { $Config.engines -contains "coeiroink" } else { $false }

$BtnSetupCi = New-Object System.Windows.Forms.Button
$BtnSetupCi.Font = $FontSmall
$BtnSetupCi.Location = New-Object System.Drawing.Point(370, 44)
$BtnSetupCi.Size = New-Object System.Drawing.Size(95, 24)
$BtnSetupCi.ForeColor = [System.Drawing.Color]::White
$BtnSetupCi.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
$BtnSetupCi.Add_Click({
    $info = Get-EngineInstallInfo
    if ($info["coeiroink"]) {
        Start-NativeCoeiroink $LblEngineStatus
        Start-Sleep -Seconds 1
        $script:DetectedEngines = Scan-AllLocalEngines
        Update-EngineCheckboxTexts
    } else {
        Show-EngineInstallOptionsDialog "COEIROINK" "https://coeiroink.com/download" "" "" $LblEngineStatus $Form
        Start-Sleep -Seconds 1
        $script:DetectedEngines = Scan-AllLocalEngines
        Update-EngineCheckboxTexts
    }
})

$ChkSv = New-Object System.Windows.Forms.CheckBox
$ChkSv.Location = New-Object System.Drawing.Point(15, 72)
$ChkSv.Size = New-Object System.Drawing.Size(345, 22)
$ChkSv.Checked = if ($Config.engines) { $Config.engines -contains "sharevox" } else { $false }

$BtnSetupSv = New-Object System.Windows.Forms.Button
$BtnSetupSv.Font = $FontSmall
$BtnSetupSv.Location = New-Object System.Drawing.Point(370, 70)
$BtnSetupSv.Size = New-Object System.Drawing.Size(95, 24)
$BtnSetupSv.ForeColor = [System.Drawing.Color]::White
$BtnSetupSv.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
$BtnSetupSv.Add_Click({
    if ($RadDocker.Checked) {
        Start-DockerEngines $false $true $LblEngineStatus
        Start-Sleep -Seconds 1
        $script:DetectedEngines = Scan-AllLocalEngines
        Update-EngineCheckboxTexts
    } else {
        $info = Get-EngineInstallInfo
        if ($info["sharevox"]) {
            Start-NativeSharevox $LblEngineStatus
            Start-Sleep -Seconds 1
            $script:DetectedEngines = Scan-AllLocalEngines
            Update-EngineCheckboxTexts
        } else {
            $svTag = Get-GitHubLatestReleaseTag "SHAREVOX/sharevox" "0.2.1"
            $svUrl = "https://github.com/SHAREVOX/sharevox/releases/download/$svTag/SHAREVOX.Web.Setup.$svTag.exe"
            Show-EngineInstallOptionsDialog "SHAREVOX" "https://www.sharevox.net/" $svUrl "SHAREVOX.Web.Setup.exe" $LblEngineStatus $Form
            Start-Sleep -Seconds 1
            $script:DetectedEngines = Scan-AllLocalEngines
            Update-EngineCheckboxTexts
        }
    }
})

Update-EngineCheckboxTexts

$ChkAutoEngine = New-Object System.Windows.Forms.CheckBox
$ChkAutoEngine.Text = "未起動のローカルエンジンを自動バックグラウンド起動 (最小化)"
$ChkAutoEngine.Location = New-Object System.Drawing.Point(15, 102)
$ChkAutoEngine.Size = New-Object System.Drawing.Size(440, 20)
$ChkAutoEngine.Checked = if ($null -ne $Config.auto_setup_engine) { [bool]$Config.auto_setup_engine } else { $true }

$LblEngineStatus = New-Object System.Windows.Forms.Label
$LblEngineStatus.Text = if ($HasAnyEngine) { "検出されたエンジンを自動認識しました" } else { "エンジン未起動。入手ボタンまたは自動起動が利用できます" }
$LblEngineStatus.Font = $FontSmall
$LblEngineStatus.ForeColor = [System.Drawing.Color]::FromArgb(88, 101, 242)
$LblEngineStatus.Location = New-Object System.Drawing.Point(15, 130)
$LblEngineStatus.Size = New-Object System.Drawing.Size(450, 20)

$GrpEngines.Controls.Add($ChkVv)
$GrpEngines.Controls.Add($BtnSetupVv)
$GrpEngines.Controls.Add($ChkCi)
$GrpEngines.Controls.Add($BtnSetupCi)
$GrpEngines.Controls.Add($ChkSv)
$GrpEngines.Controls.Add($BtnSetupSv)
$GrpEngines.Controls.Add($ChkAutoEngine)
$GrpEngines.Controls.Add($LblEngineStatus)
$Form.Controls.Add($GrpEngines)

# --- 3. PC負荷 & ゲーム配慮 グループ ---
$GrpPerf = New-Object System.Windows.Forms.GroupBox
$GrpPerf.Text = "3. PC負荷 & ゲーム配慮"
$GrpPerf.Font = $FontRegular
$GrpPerf.ForeColor = [System.Drawing.Color]::White
$GrpPerf.Location = New-Object System.Drawing.Point(20, 484)
$GrpPerf.Size = New-Object System.Drawing.Size(480, 94)

$ChkGamePriority = New-Object System.Windows.Forms.CheckBox
$ChkGamePriority.Text = "ゲーム優先モード (プロセス優先度: 通常以下) ※推奨"
$ChkGamePriority.Location = New-Object System.Drawing.Point(15, 18)
$ChkGamePriority.Size = New-Object System.Drawing.Size(440, 20)
$ChkGamePriority.Checked = if ($null -ne $Config.game_priority) { [bool]$Config.game_priority } else { $true }

$LblGameNote = New-Object System.Windows.Forms.Label
$LblGameNote.Text = "ゲームプレイ中もFPSを落とさず、余ったGPU/CPUパワーのみで安全に処理します。"
$LblGameNote.Font = $FontSmall
$LblGameNote.ForeColor = [System.Drawing.Color]::DarkGray
$LblGameNote.Location = New-Object System.Drawing.Point(35, 38)
$LblGameNote.Size = New-Object System.Drawing.Size(430, 18)

$LblCapacity = New-Object System.Windows.Forms.Label
$LblCapacity.Text = "同時処理数:"
$LblCapacity.Location = New-Object System.Drawing.Point(15, 62)
$LblCapacity.Size = New-Object System.Drawing.Size(80, 20)

$CmbCapacity = New-Object System.Windows.Forms.ComboBox
$CmbCapacity.DropDownStyle = [System.Windows.Forms.ComboBoxStyle]::DropDownList
$CmbCapacity.Items.Add("1件 (低負荷・CPU向け)") | Out-Null
$CmbCapacity.Items.Add("2件 (標準・推奨)") | Out-Null
$CmbCapacity.Items.Add("3件 (ハイパワーGPU向け)") | Out-Null
$CmbCapacity.Items.Add("4件 (最強スペック向け)") | Out-Null
$CmbCapacity.SelectedIndex = if ($Config.capacity) { [Math]::Max(0, [Math]::Min(3, $Config.capacity - 1)) } else { 1 }
$CmbCapacity.Location = New-Object System.Drawing.Point(100, 60)
$CmbCapacity.Size = New-Object System.Drawing.Size(180, 22)

$GrpPerf.Controls.Add($ChkGamePriority)
$GrpPerf.Controls.Add($LblGameNote)
$GrpPerf.Controls.Add($LblCapacity)
$GrpPerf.Controls.Add($CmbCapacity)
$Form.Controls.Add($GrpPerf)

# --- 4. ショートカット & 設定記憶 ---
$LblName = New-Object System.Windows.Forms.Label
$LblName.Text = "ノード名 (ニックネーム):"
$LblName.Font = $FontSmall
$LblName.Location = New-Object System.Drawing.Point(20, 588)
$LblName.Size = New-Object System.Drawing.Size(140, 18)
$Form.Controls.Add($LblName)

$TxtName = New-Object System.Windows.Forms.TextBox
$TxtName.Location = New-Object System.Drawing.Point(160, 586)
$TxtName.Size = New-Object System.Drawing.Size(340, 22)
$TxtName.BackColor = $ColorInputBg
$TxtName.ForeColor = [System.Drawing.Color]::White
$TxtName.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$TxtName.Text = if ($Config.node_name) { $Config.node_name } else { $env:COMPUTERNAME }
$Form.Controls.Add($TxtName)

$ChkDesktop = New-Object System.Windows.Forms.CheckBox
$ChkDesktop.Text = "デスクトップにショートカットを作成 (次回からダブルクリックで即起動)"
$ChkDesktop.Location = New-Object System.Drawing.Point(20, 614)
$ChkDesktop.Size = New-Object System.Drawing.Size(480, 20)
$ChkDesktop.Checked = $true
$Form.Controls.Add($ChkDesktop)

$ChkStartup = New-Object System.Windows.Forms.CheckBox
$ChkStartup.Text = "Windows 起動時に自動でバックグラウンド実行 (スタートアップ常駐)"
$ChkStartup.Location = New-Object System.Drawing.Point(20, 636)
$ChkStartup.Size = New-Object System.Drawing.Size(480, 20)
$ChkStartup.Checked = if ($null -ne $Config.startup) { [bool]$Config.startup } else { $false }
$Form.Controls.Add($ChkStartup)

$ChkSave = New-Object System.Windows.Forms.CheckBox
$ChkSave.Text = "この設定をローカルに記憶する"
$ChkSave.Location = New-Object System.Drawing.Point(20, 658)
$ChkSave.Size = New-Object System.Drawing.Size(480, 20)
$ChkSave.Checked = $true
$Form.Controls.Add($ChkSave)

# --- ボタン ---
$BtnCancel = New-Object System.Windows.Forms.Button
$BtnCancel.Text = "キャンセル"
$BtnCancel.Location = New-Object System.Drawing.Point(280, 695)
$BtnCancel.Size = New-Object System.Drawing.Size(100, 32)
$BtnCancel.BackColor = [System.Drawing.Color]::FromArgb(79, 84, 92)
$BtnCancel.ForeColor = [System.Drawing.Color]::White
$BtnCancel.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
$BtnCancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$Form.Controls.Add($BtnCancel)

$BtnStart = New-Object System.Windows.Forms.Button
$BtnStart.Text = "起動する"
$BtnStart.Font = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)
$BtnStart.Location = New-Object System.Drawing.Point(390, 695)
$BtnStart.Size = New-Object System.Drawing.Size(110, 32)
$BtnStart.BackColor = $ColorAccent
$BtnStart.ForeColor = [System.Drawing.Color]::White
$BtnStart.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
$Form.Controls.Add($BtnStart)

$BtnStart.Add_Click({
    # 共有トークン必須バリデーション
    if (-not $TxtToken.Text.Trim()) {
        [System.Windows.Forms.MessageBox]::Show(
            "共有トークンの入力は必須です。`nCloudflare Tunnel / インターネット経由の安全な接続には認証トークンが必要です。`n管理者から受領したトークンを入力してください。",
            "共有トークン必須",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Warning
        ) | Out-Null
        $TxtToken.Focus()
        return
    }

    if (-not $ChkVv.Checked -and -not $ChkCi.Checked -and -not $ChkSv.Checked) {
        [System.Windows.Forms.MessageBox]::Show(
            "提供する音声合成エンジンを少なくとも1つ選択してください。",
            "エンジン未選択",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Warning
        ) | Out-Null
        return
    }

    $Form.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $Form.Close()
})

$Form.AcceptButton = $BtnStart
$Form.CancelButton = $BtnCancel

# 表示
$Result = $Form.ShowDialog()

if ($Result -eq [System.Windows.Forms.DialogResult]::OK) {
    $ChosenRuntime = if ($RadDocker.Checked) { "docker" } else { "native" }

    # 自動起動オプション有効時の処理
    if ($ChkAutoEngine.Checked) {
        if ($ChosenRuntime -eq "docker") {
            Write-Host "[Docker] Docker Desktop で音声合成コンテナを起動中..." -ForegroundColor Cyan
            Start-DockerEngines $ChkVv.Checked $ChkSv.Checked
        } else {
            # ネイティブ最小化起動
            if ($ChkVv.Checked -and -not $DetectedEngines["voicevox"]) {
                Write-Host "▶ VOICEVOX を最小化起動中..." -ForegroundColor Cyan
                Start-NativeVoicevox
            }
            if ($ChkSv.Checked -and -not $DetectedEngines["sharevox"]) {
                Write-Host "▶ SHAREVOX を最小化起動中..." -ForegroundColor Cyan
                Start-NativeSharevox
            }
            # バージョン確認 (ユーザー承認付きアップデート案内)
            if ($ChkVv.Checked) {
                Check-VoicevoxUpdate
            }
        }
        Start-Sleep -Seconds 1
        $DetectedEngines = Scan-AllLocalEngines
    }

    # 選択値の取りまとめ
    $EnginesList = @()
    if ($ChkVv.Checked) { $EnginesList += "voicevox" }
    if ($ChkCi.Checked) { $EnginesList += "coeiroink" }
    if ($ChkSv.Checked) { $EnginesList += "sharevox" }

    $vvUrl = if ($DetectedEngines["voicevox"]) { $DetectedEngines["voicevox"].url } else { "http://127.0.0.1:50021" }
    $svUrl = if ($DetectedEngines["sharevox"]) {
        if ($ChkVv.Checked -and $DetectedEngines["sharevox"].port -eq 50021 -and $DetectedEngines["voicevox"]) {
            "http://127.0.0.1:50025"
        } else {
            $DetectedEngines["sharevox"].url
        }
    } else {
        if ($ChkVv.Checked) { "http://127.0.0.1:50025" } else { "http://127.0.0.1:50021" }
    }
    $ciUrl = if ($DetectedEngines["coeiroink"]) { $DetectedEngines["coeiroink"].url } else { "http://127.0.0.1:50031" }

    $EngineUrls = [ordered]@{
        voicevox  = $vvUrl
        coeiroink = $ciUrl
        sharevox  = $svUrl
    }

    $SelectedCapacity = $CmbCapacity.SelectedIndex + 1
    $ExclusiveGuildId = if ($RadDedicated.Checked) { $TxtGuild.Text.Trim() } else { "" }

    $NewConfig = [ordered]@{
        server_url         = $TxtServer.Text.Trim()
        token              = $TxtToken.Text.Trim()
        runtime            = $ChosenRuntime
        exclusive_guild_id = $ExclusiveGuildId
        engines            = $EnginesList
        engine_urls        = $EngineUrls
        auto_setup_engine  = $ChkAutoEngine.Checked
        game_priority      = $ChkGamePriority.Checked
        capacity           = $SelectedCapacity
        node_name          = $TxtName.Text.Trim()
        startup            = $ChkStartup.Checked
    }

    # 設定保存
    if ($ChkSave.Checked) {
        $JsonStr = $NewConfig | ConvertTo-Json -Depth 4
        Set-Content -Path $ConfigPath -Value $JsonStr -Encoding UTF8
        Write-Host "[OK] 設定を $ConfigPath に保存しました。" -ForegroundColor Green
    }

    # ショートカット作成ヘルパー
    function Create-LnkShortcut([string]$lnkPath, [string]$target, [string]$workDir, [string]$desc) {
        try {
            $WScriptShell = New-Object -ComObject WScript.Shell
            $Shortcut = $WScriptShell.CreateShortcut($lnkPath)
            $Shortcut.TargetPath = $target
            $Shortcut.WorkingDirectory = $workDir
            $Shortcut.WindowStyle = 7 # 7 = Minimized
            $Shortcut.Description = $desc
            $Shortcut.Save()
            return $true
        } catch {
            return $false
        }
    }

    $batPath = Join-Path $WorkerDir "run_volunteer.bat"

    # デスクトップショートカット作成
    $DesktopDir = [System.Environment]::GetFolderPath('Desktop')
    $DesktopShortcut = Join-Path $DesktopDir "TTSBot Volunteer Worker.lnk"
    if ($ChkDesktop.Checked) {
        if (Create-LnkShortcut $DesktopShortcut $batPath $WorkerDir "TTSBot Volunteer Worker (ボランティア参加ツール)") {
            Write-Host "[OK] デスクトップにショートカットを作成しました: $DesktopShortcut" -ForegroundColor Green
        }
    }

    # スタートアップ登録制御
    $StartupDir = [System.Environment]::GetFolderPath('Startup')
    $StartupShortcut = Join-Path $StartupDir "TTSBotVolunteerWorker.lnk"
    if ($ChkStartup.Checked) {
        if (Create-LnkShortcut $StartupShortcut $batPath $WorkerDir "TTSBot Volunteer Worker Agent") {
            Write-Host "[OK] Windows スタートアップに登録しました (最小化起動)" -ForegroundColor Green
        }
    } else {
        if (Test-Path $StartupShortcut) {
            Remove-Item $StartupShortcut -Force -ErrorAction SilentlyContinue
            Write-Host "[情報] スタートアップ登録を解除しました。" -ForegroundColor Yellow
        }
    }

    # 呼び出し元が run_volunteer.bat 経由かどうかを判定
    $InvokedFromBat = ($MyInvocation.Line -match "run_volunteer\.bat") -or ($env:TTSBOT_WORKER_INVOKED_BY_BAT -eq "1")

    if (-not $InvokedFromBat -and -not $PSScriptRoot) {
        # ワンライナー (irm ... | iex) 等から直接実行された場合
        Write-Host "`n[準備中] ボランティア Worker エージェントの準備中..." -ForegroundColor Green

        $serverUrl = $NewConfig.server_url
        $httpBase = $serverUrl -replace '^wss://', 'https://' -replace '^ws://', 'http://' -replace '/api/worker/ws/?$', '' -replace '/+$', ''

        foreach ($file in @("volunteer_worker.py", "run_volunteer.bat", "run_volunteer_gui.ps1")) {
            $destPath = Join-Path $WorkerDir $file
            if (-not (Test-Path $destPath)) {
                $downloadUrl = "$httpBase/volunteer/$file"
                try {
                    Write-Host "  取得中: $file ..." -ForegroundColor Cyan
                    Invoke-RestMethod -Uri $downloadUrl -OutFile $destPath -TimeoutSec 15
                } catch {
                    Write-Warning "ファイルの取得に失敗しました ($downloadUrl): $_"
                }
            }
        }

        # Python の存在確認
        $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCmd) { $pythonCmd = Get-Command python3 -ErrorAction SilentlyContinue }
        if (-not $pythonCmd) {
            [System.Windows.Forms.MessageBox]::Show(
                "Python が見つかりませんでした。`nPython 3.9 以上をインストールし、『Add Python to PATH』にチェックを入れてください。`n`nダウンロード: https://www.python.org/downloads/",
                "TTSBot Volunteer Worker",
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Error
            ) | Out-Null
            Write-Host "[エラー] Python が見つかりませんでした。Python 3.9 以上をインストールしてください。" -ForegroundColor Red
            exit 1
        }

        # aiohttp の確認 & インストール
        & $pythonCmd.Source -c "import aiohttp" 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "▶ 必要なライブラリ (aiohttp) をインストール中..." -ForegroundColor Cyan
            & $pythonCmd.Source -m pip install aiohttp
        }

        # volunteer_worker.py の起動
        $workerScript = Join-Path $WorkerDir "volunteer_worker.py"
        if (Test-Path $workerScript) {
            $pyArgs = @($workerScript, "--server-url", $NewConfig.server_url)
            if ($NewConfig.token) { $pyArgs += @("--token", $NewConfig.token) }
            if ($NewConfig.node_name) { $pyArgs += @("--name", $NewConfig.node_name) }
            if ($NewConfig.capacity) { $pyArgs += @("--capacity", [string]$NewConfig.capacity) }
            if ($NewConfig.exclusive_guild_id) { $pyArgs += @("--exclusive-guild-id", $NewConfig.exclusive_guild_id) }
            if ($NewConfig.game_priority) { $pyArgs += @("--game-priority") }
            if ($NewConfig.runtime) { $pyArgs += @("--runtime", $NewConfig.runtime) }

            $engineUrls = $NewConfig.engine_urls
            if ($engineUrls.voicevox) { $pyArgs += @("--voicevox-url", $engineUrls.voicevox) }
            if ($engineUrls.coeiroink) { $pyArgs += @("--coeiroink-url", $engineUrls.coeiroink) }
            if ($engineUrls.sharevox) { $pyArgs += @("--sharevox-url", $engineUrls.sharevox) }

            Write-Host "`n======================================================" -ForegroundColor Cyan
            Write-Host "  [TTSBot Volunteer Worker 起動完了]" -ForegroundColor Cyan
            Write-Host "======================================================" -ForegroundColor Cyan
            Write-Host "▶ サーバー      : $($NewConfig.server_url)" -ForegroundColor White
            Write-Host "▶ 共有トークン  : 設定完了 (認証保護)" -ForegroundColor White
            Write-Host "▶ 実行環境      : $($NewConfig.runtime)" -ForegroundColor White
            Write-Host "▶ ノード名      : $($NewConfig.node_name)" -ForegroundColor White
            Write-Host "▶ キャパシティ  : $($NewConfig.capacity) 並列" -ForegroundColor White
            Write-Host "▶ プロセス優先度: $(if($NewConfig.game_priority){'ゲーム優先モード (BelowNormal)'}else{'通常 (Normal)'})" -ForegroundColor White
            Write-Host "`n[接続中] ボランティアエージェントが接続を開始しました (終了するには Ctrl+C を押してください)...`n" -ForegroundColor Green

            & $pythonCmd.Source $pyArgs
        }
    }

    return $NewConfig
} else {
    Write-Host "[終了] キャンセルされました。" -ForegroundColor Yellow
    exit 0
}
