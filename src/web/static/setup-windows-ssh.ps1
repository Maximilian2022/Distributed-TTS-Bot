# ==============================================================================
# TTSBot Worker (Windows) - OpenSSH Server 自動セットアップスクリプト
# 実行方法: Windows PowerShell (管理者として実行) に本スクリプトを貼り付けて実行
# 機能:
#   - OpenSSH Server 機能の自動インストール & サービス自動起動
#   - ファイアウォール許可 (TCP ポート 22)
#   - デフォルトシェルを PowerShell に設定
#   - 共通公開鍵の自動配置 (管理者用 / ユーザー用の両方に対応)
#   - 厳格なアクセス権限 (ACL) の自動設定 (Bad ownership or permissions エラー防止)
# ==============================================================================

# 1. 管理者権限チェック
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "❌ エラー: 本スクリプトは管理者権限で実行する必要があります。" -ForegroundColor Red
    Write-Host "   PowerShell を「管理者として実行」で開き、再度実行してください。" -ForegroundColor Yellow
    exit 1
}

Write-Host "==============================================================================" -ForegroundColor Cyan
Write-Host "   🚀 TTSBot Worker (Windows) OpenSSH サーバー自動セットアップ" -ForegroundColor Cyan
Write-Host "==============================================================================" -ForegroundColor Cyan

# 登録する公開鍵 (OpenSSH 形式 - Linux 側と共通)
$PublicKey = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDokomo/DAu+9g3gB+zIEinh7ew4T9mWUMi6RB5NhLbflPNqPlCcXXXurqRCJGdcd15JGzPafPSKanW/cKrl3jzIoslcJ4DcnVVbpEhBjbDS7AcB37tn0/La3VmbJQGlRryb5u07HAuYY5HbWgdNFozLagLPlcYGi7P1k3HT0KOVlq2SznmXnnEwgvgjgV/Tm9rMHQwqQdghHlYUaNxswcRSnN/1onppVoe3t7t4xVgdDlwIaTIwZX2dG+gwYtbv1PVB8BKvRsOeCvartKevlr7kkFItbD3oOmgyHU3uRpNSxre8u3KwrSazIKyQmBQRYxhgM536M7u5OGrG1Mvtm4Z rsa-key-20210606"

# 2. OpenSSH Server 機能のインストール確認 & 追加
Write-Host "`n📦 [1/5] OpenSSH Server 機能を確認中..." -ForegroundColor Blue
$sshCapability = Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Server*'
if ($sshCapability.State -ne 'Installed') {
    Write-Host "  OpenSSH Server をインストール中... (Windows Update からダウンロードされます)" -ForegroundColor Yellow
    Add-WindowsCapability -Online -Name $sshCapability.Name | Out-Null
    Write-Host "  ✅ OpenSSH Server のインストールが完了しました。" -ForegroundColor Green
} else {
    Write-Host "  ✅ OpenSSH Server は既にインストールされています。" -ForegroundColor Green
}

# 3. OpenSSH サービスの起動 & 自動起動設定
Write-Host "`n⚙️  [2/5] sshd サービスを設定・起動中..." -ForegroundColor Blue
Start-Service sshd -ErrorAction SilentlyContinue
Set-Service -Name sshd -StartupType 'Automatic'
Start-Service "ssh-agent" -ErrorAction SilentlyContinue
Set-Service -Name "ssh-agent" -StartupType 'Automatic' -ErrorAction SilentlyContinue
Write-Host "  ✅ sshd サービスを自動起動に設定しました。" -ForegroundColor Green

# 4. ファイアウォール規則の確認・追加 (ポート 22 / すべてのネットワークプロファイルを許可)
Write-Host "`n🛡️  [3/5] ファイアウォール設定を確認中..." -ForegroundColor Blue
$fwRule = Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue
if (-not $fwRule) {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH SSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -Profile Any | Out-Null
    Write-Host "  ✅ ポート 22 の受信許可規則を追加しました (Profile: Any)。" -ForegroundColor Green
} else {
    Enable-NetFirewallRule -Name "OpenSSH-Server-In-TCP" | Out-Null
    Set-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -Profile Any | Out-Null
    Write-Host "  ✅ ポート 22 は既に許可されています (Profile: Any に更新)。" -ForegroundColor Green
}

# 5. デフォルトシェルを PowerShell に設定
Write-Host "`n🐚 [4/5] デフォルトシェルを PowerShell に設定中..." -ForegroundColor Blue
if (-not (Test-Path "HKLM:\SOFTWARE\OpenSSH")) {
    New-Item -Path "HKLM:\SOFTWARE\OpenSSH" -Force | Out-Null
}
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force | Out-Null
Write-Host "  ✅ SSH 接続時のシェルを PowerShell に設定しました。" -ForegroundColor Green

# 6. 公開鍵の配置 & 厳格なアクセス権限設定 (ACL)
Write-Host "`n🔑 [5/5] 公開鍵 (authorized_keys) を配置・権限設定中..." -ForegroundColor Blue

# A) 管理者用: C:\ProgramData\ssh\administrators_authorized_keys
$AdminKeyPath = "C:\ProgramData\ssh\administrators_authorized_keys"
$ProgramDataSsh = "C:\ProgramData\ssh"
if (-not (Test-Path $ProgramDataSsh)) {
    New-Item -ItemType Directory -Path $ProgramDataSsh -Force | Out-Null
}

$existingAdminKeys = if (Test-Path $AdminKeyPath) { Get-Content -Path $AdminKeyPath -Raw } else { "" }
if ($existingAdminKeys -notmatch [regex]::Escape($PublicKey.Trim())) {
    Add-Content -Path $AdminKeyPath -Value "`n$PublicKey" -Encoding utf8
}

# ACL 設定 (SYSTEM と Administrators のみに限定、継承無効化)
try {
    & icacls.exe $AdminKeyPath /inheritance:r /grant "SYSTEM:(F)" /grant "BUILTIN\Administrators:(F)" | Out-Null
    Write-Host "  ✅ 管理者用キー配置完了: $AdminKeyPath (権限制限済み)" -ForegroundColor Green
} catch {
    Write-Host "  ⚠️ 管理者用キー ACL 設定で警告: $_" -ForegroundColor Yellow
}

# B) カレントユーザー用: $HOME\.ssh\authorized_keys
$UserSshDir = "$HOME\.ssh"
$UserKeyPath = "$UserSshDir\authorized_keys"
if (-not (Test-Path $UserSshDir)) {
    New-Item -ItemType Directory -Path $UserSshDir -Force | Out-Null
}
$existingUserKeys = if (Test-Path $UserKeyPath) { Get-Content -Path $UserKeyPath -Raw } else { "" }
if ($existingUserKeys -notmatch [regex]::Escape($PublicKey.Trim())) {
    Add-Content -Path $UserKeyPath -Value "`n$PublicKey" -Encoding utf8
}

# ACL 設定 (カレントユーザーと SYSTEM のみに限定、継承無効化)
try {
    $currentUser = [Environment]::UserName
    & icacls.exe $UserSshDir /inheritance:r /grant "${currentUser}:(OI)(CI)(F)" /grant "SYSTEM:(OI)(CI)(F)" | Out-Null
    & icacls.exe $UserKeyPath /inheritance:r /grant "${currentUser}:(F)" /grant "SYSTEM:(F)" | Out-Null
    Write-Host "  ✅ ユーザー用キー配置完了: $UserKeyPath (権限制限済み)" -ForegroundColor Green
} catch {
    Write-Host "  ⚠️ ユーザー用キー ACL 設定で警告: $_" -ForegroundColor Yellow
}

# 設定反映のため sshd サービスを再起動
Restart-Service sshd

# 7. 接続情報の表示
$ComputerName = $env:COMPUTERNAME
$CurrentUser = [Environment]::UserName
$LocalIP = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like "192.168.11.*" } | Select-Object -ExpandProperty IPAddress -First 1)
if (-not $LocalIP) {
    $LocalIP = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | Select-Object -ExpandProperty IPAddress -First 1)
}

Write-Host "`n==============================================================================" -ForegroundColor Green
Write-Host "   🎉 Windows OpenSSH サーバーのセットアップが完了しました！" -ForegroundColor Green
Write-Host "==============================================================================" -ForegroundColor Green
Write-Host "  ホスト名     : $ComputerName" -ForegroundColor Cyan
Write-Host "  ユーザー名   : $CurrentUser" -ForegroundColor Cyan
Write-Host "  IP アドレス  : $LocalIP" -ForegroundColor Cyan
Write-Host "`nMaster サーバーや別端末からの SSH 接続コマンド例:" -ForegroundColor Yellow
Write-Host "  ssh -i WorkerAccess/id_rsa ${CurrentUser}@${LocalIP}" -ForegroundColor White
Write-Host "==============================================================================" -ForegroundColor Green
