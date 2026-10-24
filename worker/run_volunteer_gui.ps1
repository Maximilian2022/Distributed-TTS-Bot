# ==============================================================================
# TTSBot Volunteer Worker GUI 設定ランチャー (Windows ネイティブ GUI)
# ==============================================================================
# 外部依存ゼロ: PowerShell 標準の .NET Windows Forms を使用
# ==============================================================================

param(
    [string]$DefaultServerUrl = "",
    [string]$DefaultToken = ""
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ConfigPath = Join-Path $PSScriptRoot "config.json"
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
        $wait = $asyncResult.AsyncWaitHandle.WaitOne(400, $false)
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

$IsVvActive = Test-PortOpen "127.0.0.1" 50021
$IsSvActive = Test-PortOpen "127.0.0.1" 50025
$IsCiActive = Test-PortOpen "127.0.0.1" 50031

# 2. フォームの定義
$Form = New-Object System.Windows.Forms.Form
$Form.Text = "🎙️ TTSBot Volunteer Worker 設定ランチャー"
$Form.Size = New-Object System.Drawing.Size(520, 680)
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
$LblServer.Location = New-Object System.Drawing.Point(20, 70)
$LblServer.Size = New-Object System.Drawing.Size(200, 18)
$Form.Controls.Add($LblServer)

$TxtServer = New-Object System.Windows.Forms.TextBox
$TxtServer.Location = New-Object System.Drawing.Point(20, 90)
$TxtServer.Size = New-Object System.Drawing.Size(460, 24)
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
$LblToken.Location = New-Object System.Drawing.Point(20, 120)
$LblToken.Size = New-Object System.Drawing.Size(300, 18)
$Form.Controls.Add($LblToken)

$TxtToken = New-Object System.Windows.Forms.TextBox
$TxtToken.Location = New-Object System.Drawing.Point(20, 140)
$TxtToken.Size = New-Object System.Drawing.Size(460, 24)
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
$GrpScope.Location = New-Object System.Drawing.Point(20, 175)
$GrpScope.Size = New-Object System.Drawing.Size(460, 105)

$RadGlobal = New-Object System.Windows.Forms.RadioButton
$RadGlobal.Text = "全体共有プールに参加 (全サーバーの読み上げを支援・推奨)"
$RadGlobal.Location = New-Object System.Drawing.Point(15, 22)
$RadGlobal.Size = New-Object System.Drawing.Size(420, 20)
$RadGlobal.Checked = if ($Config.exclusive_guild_id) { $false } else { $true }

$RadDedicated = New-Object System.Windows.Forms.RadioButton
$RadDedicated.Text = "自サーバー専用枠 (自分が参加するサーバーの読み上げのみ処理)"
$RadDedicated.Location = New-Object System.Drawing.Point(15, 45)
$RadDedicated.Size = New-Object System.Drawing.Size(420, 20)
$RadDedicated.Checked = -not $RadGlobal.Checked

$LblGuild = New-Object System.Windows.Forms.Label
$LblGuild.Text = "専用 Discord サーバーID:"
$LblGuild.Font = $FontSmall
$LblGuild.Location = New-Object System.Drawing.Point(35, 72)
$LblGuild.Size = New-Object System.Drawing.Size(140, 18)

$TxtGuild = New-Object System.Windows.Forms.TextBox
$TxtGuild.Location = New-Object System.Drawing.Point(180, 70)
$TxtGuild.Size = New-Object System.Drawing.Size(250, 22)
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
$GrpEngines.Text = "2. 提供する音声合成エンジン (複数選択可)"
$GrpEngines.Font = $FontRegular
$GrpEngines.ForeColor = [System.Drawing.Color]::White
$GrpEngines.Location = New-Object System.Drawing.Point(20, 290)
$GrpEngines.Size = New-Object System.Drawing.Size(460, 95)

$ChkVv = New-Object System.Windows.Forms.CheckBox
$ChkVv.Text = "VOICEVOX (四国めたん・ずんだもん等) " + $(if($IsVvActive){"🟢 稼働中"}else{"⚪ 未起動"})
$ChkVv.Location = New-Object System.Drawing.Point(15, 22)
$ChkVv.Size = New-Object System.Drawing.Size(420, 20)
$ChkVv.Checked = if ($Config.engines) { $Config.engines -contains "voicevox" } else { $true }

$ChkCi = New-Object System.Windows.Forms.CheckBox
$ChkCi.Text = "COEIROINK (つくよみちゃん・クロワ等) " + $(if($IsCiActive){"🟢 稼働中"}else{"⚪ 未起動"})
$ChkCi.Location = New-Object System.Drawing.Point(15, 44)
$ChkCi.Size = New-Object System.Drawing.Size(420, 20)
$ChkCi.Checked = if ($Config.engines) { $Config.engines -contains "coeiroink" } else { $IsCiActive }

$ChkSv = New-Object System.Windows.Forms.CheckBox
$ChkSv.Text = "SHAREVOX (小春音アミ等) " + $(if($IsSvActive){"🟢 稼働中"}else{"⚪ 未起動"})
$ChkSv.Location = New-Object System.Drawing.Point(15, 66)
$ChkSv.Size = New-Object System.Drawing.Size(420, 20)
$ChkSv.Checked = if ($Config.engines) { $Config.engines -contains "sharevox" } else { $IsSvActive }

$GrpEngines.Controls.Add($ChkVv)
$GrpEngines.Controls.Add($ChkCi)
$GrpEngines.Controls.Add($ChkSv)
$Form.Controls.Add($GrpEngines)

# --- 3. PC負荷 & ゲーム配慮 グループ ---
$GrpPerf = New-Object System.Windows.Forms.GroupBox
$GrpPerf.Text = "3. PC負荷 & ゲーム配慮"
$GrpPerf.Font = $FontRegular
$GrpPerf.ForeColor = [System.Drawing.Color]::White
$GrpPerf.Location = New-Object System.Drawing.Point(20, 395)
$GrpPerf.Size = New-Object System.Drawing.Size(460, 115)

$ChkGamePriority = New-Object System.Windows.Forms.CheckBox
$ChkGamePriority.Text = "🎮 ゲーム優先モード (プロセス優先度: 通常以下) ※推奨"
$ChkGamePriority.Location = New-Object System.Drawing.Point(15, 22)
$ChkGamePriority.Size = New-Object System.Drawing.Size(420, 20)
$ChkGamePriority.Checked = if ($null -ne $Config.game_priority) { [bool]$Config.game_priority } else { $true }

$LblGameNote = New-Object System.Windows.Forms.Label
$LblGameNote.Text = "ゲーム中もFPSを落とさず、余ったGPU/CPUパワーのみで安全に処理します。"
$LblGameNote.Font = $FontSmall
$LblGameNote.ForeColor = [System.Drawing.Color]::DarkGray
$LblGameNote.Location = New-Object System.Drawing.Point(35, 42)
$LblGameNote.Size = New-Object System.Drawing.Size(400, 18)

$LblCapacity = New-Object System.Windows.Forms.Label
$LblCapacity.Text = "同時処理タスク数:"
$LblCapacity.Location = New-Object System.Drawing.Point(15, 70)
$LblCapacity.Size = New-Object System.Drawing.Size(120, 20)

$CmbCapacity = New-Object System.Windows.Forms.ComboBox
$CmbCapacity.DropDownStyle = [System.Windows.Forms.ComboBoxStyle]::DropDownList
$CmbCapacity.Items.Add("1件 (低負荷・CPU向け)") | Out-Null
$CmbCapacity.Items.Add("2件 (標準・推奨)") | Out-Null
$CmbCapacity.Items.Add("3件 (ハイパワーGPU向け)") | Out-Null
$CmbCapacity.Items.Add("4件 (最強スペック向け)") | Out-Null
$CmbCapacity.SelectedIndex = if ($Config.capacity) { [Math]::Max(0, [Math]::Min(3, $Config.capacity - 1)) } else { 1 }
$CmbCapacity.Location = New-Object System.Drawing.Point(140, 68)
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
$LblName.Location = New-Object System.Drawing.Point(20, 520)
$LblName.Size = New-Object System.Drawing.Size(180, 18)
$Form.Controls.Add($LblName)

$TxtName = New-Object System.Windows.Forms.TextBox
$TxtName.Location = New-Object System.Drawing.Point(180, 518)
$TxtName.Size = New-Object System.Drawing.Size(300, 22)
$TxtName.BackColor = $ColorInputBg
$TxtName.ForeColor = [System.Drawing.Color]::White
$TxtName.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$TxtName.Text = if ($Config.node_name) { $Config.node_name } else { $env:COMPUTERNAME }
$Form.Controls.Add($TxtName)

$ChkStartup = New-Object System.Windows.Forms.CheckBox
$ChkStartup.Text = "🔄 Windows 起動時に自動でバックグラウンド実行 (スタートアップ常駐)"
$ChkStartup.Location = New-Object System.Drawing.Point(20, 550)
$ChkStartup.Size = New-Object System.Drawing.Size(460, 20)
$ChkStartup.Checked = if ($null -ne $Config.startup) { [bool]$Config.startup } else { $true }
$Form.Controls.Add($ChkStartup)

$ChkSave = New-Object System.Windows.Forms.CheckBox
$ChkSave.Text = "💾 この設定をローカルに記憶する (次回から即起動)"
$ChkSave.Location = New-Object System.Drawing.Point(20, 575)
$ChkSave.Size = New-Object System.Drawing.Size(460, 20)
$ChkSave.Checked = $true
$Form.Controls.Add($ChkSave)

# --- ボタン ---
$BtnCancel = New-Object System.Windows.Forms.Button
$BtnCancel.Text = "キャンセル"
$BtnCancel.Location = New-Object System.Drawing.Point(260, 605)
$BtnCancel.Size = New-Object System.Drawing.Size(100, 32)
$BtnCancel.BackColor = [System.Drawing.Color]::FromArgb(79, 84, 92)
$BtnCancel.ForeColor = [System.Drawing.Color]::White
$BtnCancel.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
$BtnCancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$Form.Controls.Add($BtnCancel)

$BtnStart = New-Object System.Windows.Forms.Button
$BtnStart.Text = "起動する 🚀"
$BtnStart.Font = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)
$BtnStart.Location = New-Object System.Drawing.Point(370, 605)
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
    # 選択値の取りまとめ
    $EnginesList = @()
    if ($ChkVv.Checked) { $EnginesList += "voicevox" }
    if ($ChkCi.Checked) { $EnginesList += "coeiroink" }
    if ($ChkSv.Checked) { $EnginesList += "sharevox" }

    $SelectedCapacity = $CmbCapacity.SelectedIndex + 1
    $ExclusiveGuildId = if ($RadDedicated.Checked) { $TxtGuild.Text.Trim() } else { "" }

    $NewConfig = [ordered]@{
        server_url         = $TxtServer.Text.Trim()
        token              = $TxtToken.Text.Trim()
        exclusive_guild_id = $ExclusiveGuildId
        engines            = $EnginesList
        game_priority      = $ChkGamePriority.Checked
        capacity           = $SelectedCapacity
        node_name          = $TxtName.Text.Trim()
        startup            = $ChkStartup.Checked
        auto_download      = $true
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
            $Shortcut.TargetPath = Join-Path $PSScriptRoot "run_volunteer.bat"
            $Shortcut.WorkingDirectory = $PSScriptRoot
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

    # 呼び出し元バッチ用に出力
    return $NewConfig
} else {
    Write-Host "🛑 キャンセルされました。" -ForegroundColor Yellow
    exit 0
}
