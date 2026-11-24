# 为 Antigravity / MCP 准备 dsh-ssh-otp 运行时与本地配置（Windows PowerShell）
#
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup-mcp.ps1
#
# 创建：
#   %USERPROFILE%\.ssh-otp\venv\Scripts\python.exe（复用 setup-python.ps1 的候选探测和 TLS 自检）
#   %USERPROFILE%\.ssh-otp\config.json（不存在才创建，绝不覆盖）
#   %USERPROFILE%\.ssh-otp\secrets.json（不存在才创建为 {}，并限制为当前用户可读写）

$ErrorActionPreference = 'Stop'

$root = Join-Path $HOME '.ssh-otp'
$venv = Join-Path $root 'venv'
$config = Join-Path $root 'config.json'
$secrets = Join-Path $root 'secrets.json'

New-Item -ItemType Directory -Force -Path $root | Out-Null
$env:DSH_SSH_OTP_VENV = $venv
& (Join-Path $PSScriptRoot 'setup-python.ps1')

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
if (-not (Test-Path $config)) {
    $initial = @{
        timeoutMs = 120000
        retryOnAuthFailure = $false
        hosts = @{}
    } | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText($config, $initial + "`n", $utf8NoBom)
}
if (-not (Test-Path $secrets)) {
    [System.IO.File]::WriteAllText($secrets, "{}`n", $utf8NoBom)
}

# 只允许当前 Windows 用户读写 secrets；不修改已有文件内容。
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $secrets /inheritance:r /grant:r "${user}:(F)" | Out-Null

Write-Host ""
Write-Host "下一步："
Write-Host "  1. 编辑 $config，添加 hosts"
Write-Host "  2. 编辑 $secrets，按 passwordRef/totpRef 填入密码与种子"
Write-Host "  3. 把 mcp/examples/mcp_config.windows.json 中的路径改成你的实际路径，合并到 ~/.gemini/config/mcp_config.json"
Write-Host "  4. 在 Antigravity 的 Installed MCP servers 中刷新，然后调用 ssh_otp_doctor"
