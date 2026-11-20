# 为 dsh-ssh-otp 准备 Python 运行时（Windows PowerShell）
#
# 用法：在 PowerShell 里执行
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup-python.ps1
#
# 它会在默认位置创建虚拟环境并安装 paramiko，插件无需改配置即可使用：
#   %USERPROFILE%\.dsh\runtimes\dsh-ssh-otp\Scripts\python.exe
#
# 想装到别处：先设置环境变量 DSH_SSH_OTP_VENV 再运行。

$ErrorActionPreference = 'Stop'

$venv = $env:DSH_SSH_OTP_VENV
if ([string]::IsNullOrWhiteSpace($venv)) {
    $venv = Join-Path $HOME '.dsh\runtimes\dsh-ssh-otp'
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "找不到 python。请先安装 Python 3.10+ 并勾选 Add python.exe to PATH。"
}

if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    Write-Host "创建虚拟环境：$venv"
    & $python.Source -m venv $venv
}

$venvPython = Join-Path $venv 'Scripts\python.exe'
Write-Host "安装 paramiko …"
& $venvPython -m pip install --upgrade pip | Out-Null
& $venvPython -m pip install --upgrade paramiko

& $venvPython -c "import paramiko; print('paramiko', paramiko.__version__, 'ok')"
Write-Host ""
Write-Host "完成。插件默认会使用：$venvPython"
