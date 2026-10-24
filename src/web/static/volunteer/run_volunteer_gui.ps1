# ==============================================================================
# TTSBot Volunteer Worker GUI 設定ランチャー (Windows ネイティブ GUI)
# ==============================================================================
# 外部依存ゼロ: PowerShell 標準の .NET Windows Forms を使用
# 特徴:
#   - 稼働中エンジンのスマートAPI判定 (VOICEVOX / SHAREVOX / COEIROINK のデフォルトポート対応)
#   - VOICEVOX の自動セットアップ & 自動バックグラウンド起動 (Docker / winget / 既存アプリ)
#   - ゲーム優先モード (BelowNormal プロセス優先度設定)
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

# 1. ローカルエンジンの簡易死活検査 (ポートチェック)
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

# 2. 各ポートで稼働しているエンジン種別のスマートAPI判定
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

    # B. COEIROINK v1/v2 確認 (/v1/speakers)
    try {
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/v1/speakers")
        $req.Timeout = 800
        $resp = $req.GetResponse()
        $resp.Close()
        return @{ type = "coeiroink"; port = $Port; url = "http://127.0.0.1:$Port"; label = "COEIROINK (ポート $Port)" }
    } catch {}

    # C. /speakers による話者判別フォールバック
    try {
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/speakers")
        $req.Timeout = 800
        $resp = $req.GetResponse()
        $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
        $body = $sr.ReadToEnd()
        $resp.Close()
        if ($body -match '(?i)小春音アミ|sharevox') {
            return @{ type = "sharevox"; port = $Port; url = "http://127.0.0.1:$Port"; label = "SHAREVOX (ポート $Port)" }
        }
        return @{ type = "voicevox"; port = $Port; url = "http://127.0.0.1:$Port"; label = "VOICEVOX (ポート $Port)" }
    } catch {}

    return @{ type = "unknown"; port = $Port; url = "http://127.0.0.1:$Port"; label = "不明なエンジン (ポート $Port)" }
}

function Scan-AllLocalEngines {
    $found = @{}
    # 探索対象ポート: 50021 (VOICEVOX/SHAREVOX公式デフォルト), 50025 (旧SHAREVOX), 50031 (COEIROINK公式デフォルト), 50032 (COEIROINK v2)
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

# 3. VOICEVOX の自動セットアップ & 自動起動
function Start-VoicevoxEngine([System.Windows.Forms.Label]$statusLabel = $null) {
    if (Test-PortOpen "127.0.0.1" 50021) {
        if ($statusLabel) { $statusLabel.Text = "✅ VOICEVOX は既に起動しています (50021)" }
        Write-Host "✅ VOICEVOX は既に起動しています (50021)" -ForegroundColor Green
        return $true
    }

    if ($statusLabel) { $statusLabel.Text = "🔍 インストール済み VOICEVOX を探索中..." }
    Write-Host "🔍 インストール済み VOICEVOX を探索中..." -ForegroundColor Cyan

    # 1. 既存のインストール済みアプリを検索して起動
    $PossiblePaths = @(
        (Join-Path $env:LOCALAPPDATA "Programs\VOICEVOX\VOICEVOX.exe"),
        "C:\Program Files\VOICEVOX\VOICEVOX.exe",
        "C:\Program Files (x86)\VOICEVOX\VOICEVOX.exe"
    )
    foreach ($p in $PossiblePaths) {
        if (Test-Path $p) {
            if ($statusLabel) { $statusLabel.Text = "🚀 VOICEVOX をバックグラウンド起動中..." }
            Write-Host "💡 インストール済みの VOICEVOX を発見: $p" -ForegroundColor Green
            Start-Process -FilePath $p -WindowStyle Minimized
            for ($i = 0; $i -lt 15; $i++) {
                Start-Sleep -Seconds 1
                if (Test-PortOpen "127.0.0.1" 50021) {
                    if ($statusLabel) { $statusLabel.Text = "✅ VOICEVOX の起動を確認しました！" }
                    Write-Host "✅ VOICEVOX の起動を確認しました (50021)" -ForegroundColor Green
                    return $true
                }
            }
        }
    }

    # 2. Docker があればコンテナで起動
    if ($statusLabel) { $statusLabel.Text = "🐳 Docker 環境を確認中..." }
    $hasDocker = $false
    try {
        $res = docker ps 2>&1
        if ($LASTEXITCODE -eq 0) { $hasDocker = $true }
    } catch {}

    if ($hasDocker) {
        if ($statusLabel) { $statusLabel.Text = "🐳 Docker で VOICEVOX コンテナを起動中..." }
        Write-Host "🐳 Docker を検出。VOICEVOX コンテナを起動します..." -ForegroundColor Cyan
        docker run -d --name ttsbot-worker-voicevox --restart unless-stopped -p 50021:50021 voicevox/voicevox_engine:cpu-latest 2>&1 | Out-Null
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Seconds 1
            if (Test-PortOpen "127.0.0.1" 50021) {
                if ($statusLabel) { $statusLabel.Text = "✅ Docker 版 VOICEVOX が起動しました！" }
                Write-Host "✅ Docker 版 VOICEVOX が起動しました！" -ForegroundColor Green
                return $true
            }
        }
    }

    # 3. winget による自動インストール (Windows 10 / 11)
    if ($statusLabel) { $statusLabel.Text = "📦 winget での公式 VOICEVOX 自動インストールを確認中..." }
    $hasWinget = $false
    try {
        $w = winget --version 2>&1
        if ($LASTEXITCODE -eq 0) { $hasWinget = $true }
    } catch {}

    if ($hasWinget) {
        if ($statusLabel) { $statusLabel.Text = "📦 winget で VOICEVOX を自動インストール中 (初回のみ数分)..." }
        Write-Host "📦 winget を使用して公式 VOICEVOX を自動インストール中..." -ForegroundColor Cyan
        try {
            winget install --id Hiroshiba.VOICEVOX --source winget --accept-source-agreements --accept-package-agreements --silent --disable-interactivity 2>&1 | Out-Null
            Start-Sleep -Seconds 2
            foreach ($p in $PossiblePaths) {
                if (Test-Path $p) {
                    Start-Process -FilePath $p -WindowStyle Minimized
                    for ($i = 0; $i -lt 15; $i++) {
                        Start-Sleep -Seconds 1
                        if (Test-PortOpen "127.0.0.1" 50021) {
                            if ($statusLabel) { $statusLabel.Text = "✅ インストール完了 & 起動を確認しました！" }
                            Write-Host "✅ VOICEVOX の自動インストール & 起動に成功しました！" -ForegroundColor Green
                            return $true
                        }
                    }
                }
            }
        } catch {
            Write-Warning "winget での自動インストールに失敗: $_"
        }
    }

    if ($statusLabel) { $statusLabel.Text = "⚠️ 自動セットアップ失敗: 公式サイトから起動してください" }
    Write-Warning "⚠️ 自動起動できませんでした。公式サイト (https://voicevox.hiroshiba.jp/) から手動起動してください。"
    return $false
}

# バッチ等のバックグラウンドからエンジン起動のみを要求された場合
if ($AutoStartEngineOnly) {
    $started = Start-VoicevoxEngine
    exit $(if ($started) { 0 } else { 1 })
}

# --- 初期エンジンスキャン ---
$DetectedEngines = Scan-AllLocalEngines
$HasAnyEngine = $DetectedEngines.Count -gt 0

# 4. フォームの定義
$Form = New-Object System.Windows.Forms.Form
$Form.Text = "🎙️ TTSBot Volunteer Worker 設定ランチャー"
$Form.Size = New-Object System.Drawing.Size(530, 730)
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
$Header.Text = "🎙️ TTSBot ボランティア参加設定"
$Header.Font = $FontTitle
$Header.Location = New-Object System.Drawing.Point(20, 15)
$Header.Size = New-Object System.Drawing.Size(460, 24)
$Form.Controls.Add($Header)

$SubHeader = New-Object System.Windows.Forms.Label
$SubHeader.Text = "PCの余剰パワーを提供してDiscord読み上げを高速化・支援します。"
$SubHeader.Font = $FontSmall
$SubHeader.ForeColor = [System.Drawing.Color]::LightGray
$SubHeader.Location = New-Object System.Drawing.Point(22, 40)
$SubHeader.Size = New-Object System.Drawing.Size(460, 18)
$Form.Controls.Add($SubHeader)

# --- 接続情報グループ ---
$LblServer = New-Object System.Windows.Forms.Label
$LblServer.Text = "接続先サーバー URL (WSS):"
$LblServer.Font = $FontRegular
$LblServer.Location = New-Object System.Drawing.Point(20, 65)
$LblServer.Size = New-Object System.Drawing.Size(200, 18)
$Form.Controls.Add($LblServer)

$TxtServer = New-Object System.Windows.Forms.TextBox
$TxtServer.Location = New-Object System.Drawing.Point(20, 85)
$TxtServer.Size = New-Object System.Drawing.Size(470, 24)
$TxtServer.BackColor = $ColorInputBg
$TxtServer.ForeColor = [System.Drawing.Color]::White
$TxtServer.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$DefaultUrlEnv = if ($env:TTSBOT_SERVER_URL) { $env:TTSBOT_SERVER_URL } else { "" }
$DefaultTokenEnv = if ($env:TTSBOT_WORKER_TOKEN) { $env:TTSBOT_WORKER_TOKEN } else { "" }
$TxtServer.Text = if ($Config.server_url) { $Config.server_url } elseif ($DefaultServerUrl) { $DefaultServerUrl } elseif ($DefaultUrlEnv) { $DefaultUrlEnv } else { "wss://<あなたのTTSBotドメイン>/api/worker/ws" }
$Form.Controls.Add($TxtServer)

$LblToken = New-Object System.Windows.Forms.Label
$LblToken.Text = "共有トークン (管理者から受領した場合のみ):"
$LblToken.Font = $FontRegular
$LblToken.Location = New-Object System.Drawing.Point(20, 115)
$LblToken.Size = New-Object System.Drawing.Size(300, 18)
$Form.Controls.Add($LblToken)

$TxtToken = New-Object System.Windows.Forms.TextBox
$TxtToken.Location = New-Object System.Drawing.Point(20, 135)
$TxtToken.Size = New-Object System.Drawing.Size(470, 24)
$TxtToken.BackColor = $ColorInputBg
$TxtToken.ForeColor = [System.Drawing.Color]::White
$TxtToken.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$TxtToken.Text = if ($Config.token) { $Config.token } elseif ($DefaultToken) { $DefaultToken } else { $DefaultTokenEnv }
$Form.Controls.Add($TxtToken)

# --- 1. 提供範囲 (スコープ) グループ ---
$GrpScope = New-Object System.Windows.Forms.GroupBox
$GrpScope.Text = "1. 提供範囲 (スコープ)"
$GrpScope.Font = $FontRegular
$GrpScope.ForeColor = [System.Drawing.Color]::White
$GrpScope.Location = New-Object System.Drawing.Point(20, 168)
$GrpScope.Size = New-Object System.Drawing.Size(470, 100)

$RadGlobal = New-Object System.Windows.Forms.RadioButton
$RadGlobal.Text = "全体共有プールに参加 (全サーバーの読み上げを支援・推奨)"
$RadGlobal.Location = New-Object System.Drawing.Point(15, 20)
$RadGlobal.Size = New-Object System.Drawing.Size(430, 20)
$RadGlobal.Checked = if ($Config.exclusive_guild_id) { $false } else { $true }

$RadDedicated = New-Object System.Windows.Forms.RadioButton
$RadDedicated.Text = "自サーバー専用枠 (自分が参加するサーバーの読み上げのみ処理)"
$RadDedicated.Location = New-Object System.Drawing.Point(15, 42)
$RadDedicated.Size = New-Object System.Drawing.Size(430, 20)
$RadDedicated.Checked = -not $RadGlobal.Checked

$LblGuild = New-Object System.Windows.Forms.Label
$LblGuild.Text = "専用 Discord サーバーID:"
$LblGuild.Font = $FontSmall
$LblGuild.Location = New-Object System.Drawing.Point(35, 68)
$LblGuild.Size = New-Object System.Drawing.Size(140, 18)

$TxtGuild = New-Object System.Windows.Forms.TextBox
$TxtGuild.Location = New-Object System.Drawing.Point(180, 66)
$TxtGuild.Size = New-Object System.Drawing.Size(260, 22)
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
$GrpEngines.Text = "2. 提供する音声合成エンジン (自動検出 & 自動セットアップ)"
$GrpEngines.Font = $FontRegular
$GrpEngines.ForeColor = [System.Drawing.Color]::White
$GrpEngines.Location = New-Object System.Drawing.Point(20, 275)
$GrpEngines.Size = New-Object System.Drawing.Size(470, 145)

# エンジンチェックボックスのテキスト更新ヘルパー
function Update-EngineCheckboxTexts {
    $vvInfo = $DetectedEngines["voicevox"]
    $ciInfo = $DetectedEngines["coeiroink"]
    $svInfo = $DetectedEngines["sharevox"]

    $ChkVv.Text = "VOICEVOX (四国めたん・ずんだもん等) " + $(if($vvInfo){"🟢 稼働中 ($($vvInfo.port))"}else{"⚪ 未起動"})
    $ChkCi.Text = "COEIROINK (つくよみちゃん・クロワ等) " + $(if($ciInfo){"🟢 稼働中 ($($ciInfo.port))"}else{"⚪ 未起動"})
    
    $svText = "SHAREVOX (小春音アミ等) " + $(if($svInfo){"🟢 稼働中 ($($svInfo.port))"}else{"⚪ 未起動"})
    if ($ChkVv.Checked -and $ChkSv.Checked -and (-not $svInfo -or $svInfo.port -eq 50021)) {
        $svText += " (同時提供: 50025自動分離)"
    }
    $ChkSv.Text = $svText
}

$ChkVv = New-Object System.Windows.Forms.CheckBox
$ChkVv.Location = New-Object System.Drawing.Point(15, 22)
$ChkVv.Size = New-Object System.Drawing.Size(430, 20)
$ChkVv.Checked = if ($Config.engines) { $Config.engines -contains "voicevox" } else { $true }

$ChkCi = New-Object System.Windows.Forms.CheckBox
$ChkCi.Location = New-Object System.Drawing.Point(15, 43)
$ChkCi.Size = New-Object System.Drawing.Size(430, 20)
$ChkCi.Checked = if ($Config.engines) { $Config.engines -contains "coeiroink" } else { [bool]$DetectedEngines["coeiroink"] }

$ChkSv = New-Object System.Windows.Forms.CheckBox
$ChkSv.Location = New-Object System.Drawing.Point(15, 64)
$ChkSv.Size = New-Object System.Drawing.Size(430, 20)
$ChkSv.Checked = if ($Config.engines) { $Config.engines -contains "sharevox" } else { [bool]$DetectedEngines["sharevox"] }

$ChkVv.Add_CheckedChanged({ Update-EngineCheckboxTexts })
$ChkSv.Add_CheckedChanged({ Update-EngineCheckboxTexts })

Update-EngineCheckboxTexts

# 自動セットアップ制御
$ChkAutoEngine = New-Object System.Windows.Forms.CheckBox
$ChkAutoEngine.Text = "🚀 未起動時は VOICEVOX を自動インストール・起動する (推奨)"
$ChkAutoEngine.Font = $FontSmall
$ChkAutoEngine.ForeColor = [System.Drawing.Color]::FromArgb(56, 189, 248)
$ChkAutoEngine.Location = New-Object System.Drawing.Point(15, 88)
$ChkAutoEngine.Size = New-Object System.Drawing.Size(320, 20)
$ChkAutoEngine.Checked = if ($null -ne $Config.auto_setup_engine) { [bool]$Config.auto_setup_engine } else { -not $DetectedEngines["voicevox"] }

$BtnInstallEngine = New-Object System.Windows.Forms.Button
$BtnInstallEngine.Text = "⚡ 今すぐ自動起動"
$BtnInstallEngine.Font = $FontSmall
$BtnInstallEngine.Location = New-Object System.Drawing.Point(340, 86)
$BtnInstallEngine.Size = New-Object System.Drawing.Size(120, 24)
$BtnInstallEngine.BackColor = [System.Drawing.Color]::FromArgb(40, 167, 69)
$BtnInstallEngine.ForeColor = [System.Drawing.Color]::White
$BtnInstallEngine.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat

$LblEngineStatus = New-Object System.Windows.Forms.Label
$LblEngineStatus.Text = if ($HasAnyEngine) { "✨ 検出完了: ボランティア参加準備が整っています" } else { "※ エンジンが未起動です。「今すぐ自動起動」または起動後にご参加ください" }
$LblEngineStatus.Font = $FontSmall
$LblEngineStatus.ForeColor = [System.Drawing.Color]::DarkGray
$LblEngineStatus.Location = New-Object System.Drawing.Point(15, 115)
$LblEngineStatus.Size = New-Object System.Drawing.Size(440, 18)

$BtnInstallEngine.Add_Click({
    $BtnInstallEngine.Enabled = $false
    $LblEngineStatus.ForeColor = [System.Drawing.Color]::Yellow
    Start-VoicevoxEngine $LblEngineStatus
    Start-Sleep -Seconds 1
    # 再スキャン
    $script:DetectedEngines = Scan-AllLocalEngines
    Update-EngineCheckboxTexts
    if ($DetectedEngines["voicevox"]) {
        $ChkVv.Checked = $true
        $LblEngineStatus.ForeColor = [System.Drawing.Color]::LightGreen
    }
    $BtnInstallEngine.Enabled = $true
})

$GrpEngines.Controls.Add($ChkVv)
$GrpEngines.Controls.Add($ChkCi)
$GrpEngines.Controls.Add($ChkSv)
$GrpEngines.Controls.Add($ChkAutoEngine)
$GrpEngines.Controls.Add($BtnInstallEngine)
$GrpEngines.Controls.Add($LblEngineStatus)
$Form.Controls.Add($GrpEngines)

# --- 3. PC負荷 & ゲーム配慮 グループ ---
$GrpPerf = New-Object System.Windows.Forms.GroupBox
$GrpPerf.Text = "3. PC負荷 & ゲーム配慮"
$GrpPerf.Font = $FontRegular
$GrpPerf.ForeColor = [System.Drawing.Color]::White
$GrpPerf.Location = New-Object System.Drawing.Point(20, 428)
$GrpPerf.Size = New-Object System.Drawing.Size(470, 110)

$ChkGamePriority = New-Object System.Windows.Forms.CheckBox
$ChkGamePriority.Text = "🎮 ゲーム優先モード (プロセス優先度: 通常以下) ※推奨"
$ChkGamePriority.Location = New-Object System.Drawing.Point(15, 20)
$ChkGamePriority.Size = New-Object System.Drawing.Size(430, 20)
$ChkGamePriority.Checked = if ($null -ne $Config.game_priority) { [bool]$Config.game_priority } else { $true }

$LblGameNote = New-Object System.Windows.Forms.Label
$LblGameNote.Text = "ゲーム中もFPSを落とさず、余ったGPU/CPUパワーのみで安全に処理します。"
$LblGameNote.Font = $FontSmall
$LblGameNote.ForeColor = [System.Drawing.Color]::DarkGray
$LblGameNote.Location = New-Object System.Drawing.Point(35, 40)
$LblGameNote.Size = New-Object System.Drawing.Size(410, 18)

$LblCapacity = New-Object System.Windows.Forms.Label
$LblCapacity.Text = "同時処理タスク数:"
$LblCapacity.Location = New-Object System.Drawing.Point(15, 68)
$LblCapacity.Size = New-Object System.Drawing.Size(120, 20)

$CmbCapacity = New-Object System.Windows.Forms.ComboBox
$CmbCapacity.DropDownStyle = [System.Windows.Forms.ComboBoxStyle]::DropDownList
$CmbCapacity.Items.Add("1件 (低負荷・CPU向け)") | Out-Null
$CmbCapacity.Items.Add("2件 (標準・推奨)") | Out-Null
$CmbCapacity.Items.Add("3件 (ハイパワーGPU向け)") | Out-Null
$CmbCapacity.Items.Add("4件 (最強スペック向け)") | Out-Null
$CmbCapacity.SelectedIndex = if ($Config.capacity) { [Math]::Max(0, [Math]::Min(3, $Config.capacity - 1)) } else { 1 }
$CmbCapacity.Location = New-Object System.Drawing.Point(140, 66)
$CmbCapacity.Size = New-Object System.Drawing.Size(180, 22)

$GrpPerf.Controls.Add($ChkGamePriority)
$GrpPerf.Controls.Add($LblGameNote)
$GrpPerf.Controls.Add($LblCapacity)
$GrpPerf.Controls.Add($CmbCapacity)
$Form.Controls.Add($GrpPerf)

# --- 4. 常駐 & 設定記憶 ---
$LblName = New-Object System.Windows.Forms.Label
$LblName.Text = "表示ノード名 (ニックネーム):"
$LblName.Font = $FontSmall
$LblName.Location = New-Object System.Drawing.Point(20, 548)
$LblName.Size = New-Object System.Drawing.Size(180, 18)
$Form.Controls.Add($LblName)

$TxtName = New-Object System.Windows.Forms.TextBox
$TxtName.Location = New-Object System.Drawing.Point(180, 546)
$TxtName.Size = New-Object System.Drawing.Size(310, 22)
$TxtName.BackColor = $ColorInputBg
$TxtName.ForeColor = [System.Drawing.Color]::White
$TxtName.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$TxtName.Text = if ($Config.node_name) { $Config.node_name } else { $env:COMPUTERNAME }
$Form.Controls.Add($TxtName)

$ChkStartup = New-Object System.Windows.Forms.CheckBox
$ChkStartup.Text = "🔄 Windows 起動時に自動でバックグラウンド実行 (スタートアップ常駐)"
$ChkStartup.Location = New-Object System.Drawing.Point(20, 574)
$ChkStartup.Size = New-Object System.Drawing.Size(470, 20)
$ChkStartup.Checked = if ($null -ne $Config.startup) { [bool]$Config.startup } else { $true }
$Form.Controls.Add($ChkStartup)

$ChkSave = New-Object System.Windows.Forms.CheckBox
$ChkSave.Text = "💾 この設定をローカルに記憶する (次回から即起動)"
$ChkSave.Location = New-Object System.Drawing.Point(20, 598)
$ChkSave.Size = New-Object System.Drawing.Size(470, 20)
$ChkSave.Checked = $true
$Form.Controls.Add($ChkSave)

# --- ボタン ---
$BtnCancel = New-Object System.Windows.Forms.Button
$BtnCancel.Text = "キャンセル"
$BtnCancel.Location = New-Object System.Drawing.Point(270, 635)
$BtnCancel.Size = New-Object System.Drawing.Size(100, 32)
$BtnCancel.BackColor = [System.Drawing.Color]::FromArgb(79, 84, 92)
$BtnCancel.ForeColor = [System.Drawing.Color]::White
$BtnCancel.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
$BtnCancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$Form.Controls.Add($BtnCancel)

$BtnStart = New-Object System.Windows.Forms.Button
$BtnStart.Text = "起動する 🚀"
$BtnStart.Font = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)
$BtnStart.Location = New-Object System.Drawing.Point(380, 635)
$BtnStart.Size = New-Object System.Drawing.Size(110, 32)
$BtnStart.BackColor = $ColorAccent
$BtnStart.ForeColor = [System.Drawing.Color]::White
$BtnStart.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
$BtnStart.DialogResult = [System.Windows.Forms.DialogResult]::OK
$Form.Controls.Add($BtnStart)

$Form.AcceptButton = $BtnStart
$Form.CancelButton = $BtnCancel

# 表示
$Result = $Form.ShowDialog()

if ($Result -eq [System.Windows.Forms.DialogResult]::OK) {
    # 自動起動オプションが有効で、かつVOICEVOX未起動なら今すぐ起動
    if ($ChkAutoEngine.Checked -and -not $DetectedEngines["voicevox"]) {
        Write-Host "▶ VOICEVOX を自動起動中..." -ForegroundColor Cyan
        Start-VoicevoxEngine
        Start-Sleep -Seconds 1
        $DetectedEngines = Scan-AllLocalEngines
    }

    # 選択値の取りまとめ
    $EnginesList = @()
    if ($ChkVv.Checked) { $EnginesList += "voicevox" }
    if ($ChkCi.Checked) { $EnginesList += "coeiroink" }
    if ($ChkSv.Checked) { $EnginesList += "sharevox" }

    # 検出された実際の URL (VOICEVOX と SHAREVOX のポート 50021 衝突を自動回避)
    $vvUrl = if ($DetectedEngines["voicevox"]) { $DetectedEngines["voicevox"].url } else { "http://127.0.0.1:50021" }
    $svUrl = if ($DetectedEngines["sharevox"]) {
        # 既に 50025 または別ポートで動いていればそのポート、もし 50021 で VOICEVOX と被るなら 50025 を優先
        if ($ChkVv.Checked -and $DetectedEngines["sharevox"].port -eq 50021 -and $DetectedEngines["voicevox"]) {
            "http://127.0.0.1:50025"
        } else {
            $DetectedEngines["sharevox"].url
        }
    } else {
        # VOICEVOX と同時選択の場合は 50025 に自動分離
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
        Write-Host "✅ 設定を $ConfigPath に保存しました。" -ForegroundColor Green
    }

    # スタートアップ登録制御
    $StartupDir = [System.Environment]::GetFolderPath('Startup')
    $ShortcutPath = Join-Path $StartupDir "TTSBotVolunteerWorker.lnk"

    if ($ChkStartup.Checked) {
        try {
            $WScriptShell = New-Object -ComObject WScript.Shell
            $Shortcut = $WScriptShell.CreateShortcut($ShortcutPath)
            $Shortcut.TargetPath = Join-Path $WorkerDir "run_volunteer.bat"
            $Shortcut.WorkingDirectory = $WorkerDir
            $Shortcut.WindowStyle = 7 # 7 = Minimized (最小化)
            $Shortcut.Description = "TTSBot Volunteer Worker Agent"
            $Shortcut.Save()
            Write-Host "✅ Windows スタートアップに登録しました (最小化起動)" -ForegroundColor Green
        } catch {
            Write-Warning "スタートアップ登録に失敗しました: $_"
        }
    } else {
        if (Test-Path $ShortcutPath) {
            Remove-Item $ShortcutPath -Force -ErrorAction SilentlyContinue
            Write-Host "ℹ️ スタートアップ登録を解除しました。" -ForegroundColor Yellow
        }
    }

    # 呼び出し元が run_volunteer.bat 経由かどうかを判定
    $InvokedFromBat = ($MyInvocation.Line -match "run_volunteer\.bat") -or ($env:TTSBOT_WORKER_INVOKED_BY_BAT -eq "1")

    if (-not $InvokedFromBat -and -not $PSScriptRoot) {
        # ワンライナー (irm ... | iex) 等から直接 PowerShell で実行された場合:
        # 必要なエージェントファイルを ~/.ttsbot-worker に自動展開し、直接起動する
        Write-Host "`n🚀 ボランティア Worker エージェントの準備中..." -ForegroundColor Green

        # 1. サーバーURLから HTTP ベースURLを特定
        $serverUrl = $NewConfig.server_url
        $httpBase = $serverUrl -replace '^wss://', 'https://' -replace '^ws://', 'http://' -replace '/api/worker/ws/?$', '' -replace '/+$', ''

        # 2. 必要なスクリプト (volunteer_worker.py, run_volunteer.bat) の自動取得
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

        # 3. Python の存在確認
        $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCmd) {
            $pythonCmd = Get-Command python3 -ErrorAction SilentlyContinue
        }
        if (-not $pythonCmd) {
            [System.Windows.Forms.MessageBox]::Show(
                "Python が見つかりませんでした。`nPython 3.9 以上をインストールし、『Add Python to PATH』にチェックを入れてください。`n`nダウンロード: https://www.python.org/downloads/",
                "TTSBot Volunteer Worker",
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Error
            ) | Out-Null
            Write-Host "❌ Python が見つかりませんでした。Python 3.9 以上をインストールしてください。" -ForegroundColor Red
            exit 1
        }

        # 4. aiohttp の存在確認 & インストール
        & $pythonCmd.Source -c "import aiohttp" 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "▶ 必要なライブラリ (aiohttp) をインストール中..." -ForegroundColor Cyan
            & $pythonCmd.Source -m pip install aiohttp
        }

        # 5. volunteer_worker.py の起動
        $workerScript = Join-Path $WorkerDir "volunteer_worker.py"
        if (Test-Path $workerScript) {
            $pyArgs = @($workerScript, "--server-url", $NewConfig.server_url)
            if ($NewConfig.token) { $pyArgs += @("--token", $NewConfig.token) }
            if ($NewConfig.node_name) { $pyArgs += @("--name", $NewConfig.node_name) }
            if ($NewConfig.capacity) { $pyArgs += @("--capacity", [string]$NewConfig.capacity) }
            if ($NewConfig.exclusive_guild_id) { $pyArgs += @("--exclusive-guild-id", $NewConfig.exclusive_guild_id) }
            if ($NewConfig.game_priority) { $pyArgs += @("--game-priority") }

            $engineUrls = $NewConfig.engine_urls
            if ($engineUrls.voicevox) { $pyArgs += @("--voicevox-url", $engineUrls.voicevox) }
            if ($engineUrls.coeiroink) { $pyArgs += @("--coeiroink-url", $engineUrls.coeiroink) }
            if ($engineUrls.sharevox) { $pyArgs += @("--sharevox-url", $engineUrls.sharevox) }

            Write-Host "`n======================================================" -ForegroundColor Cyan
            Write-Host "  🎙️ TTSBot Volunteer Worker 起動完了" -ForegroundColor Cyan
            Write-Host "======================================================" -ForegroundColor Cyan
            Write-Host "▶ サーバー      : $($NewConfig.server_url)" -ForegroundColor White
            Write-Host "▶ ノード名      : $($NewConfig.node_name)" -ForegroundColor White
            Write-Host "▶ キャパシティ  : $($NewConfig.capacity) 並列" -ForegroundColor White
            Write-Host "▶ プロセス優先度: $(if($NewConfig.game_priority){'ゲーム優先モード (BelowNormal)'}else{'通常 (Normal)'})" -ForegroundColor White
            Write-Host "`n✨ ボランティアエージェントが接続を開始しました (終了するには Ctrl+C を押してください)...`n" -ForegroundColor Green

            & $pythonCmd.Source $pyArgs
        } else {
            Write-Host "❌ volunteer_worker.py が見つかりませんでした。" -ForegroundColor Red
        }
    }

    return $NewConfig
} else {
    Write-Host "🛑 キャンセルされました。" -ForegroundColor Yellow
    exit 0
}
