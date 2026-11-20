# 为 dsh-ssh-otp 准备 Python 运行时（Windows PowerShell）
#
# 用法：在 PowerShell 里执行
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup-python.ps1
#
# 行为：
#   1. 依次探测候选解释器：环境变量 DSH_SSH_OTP_BASE_PYTHON → DSH 自带运行时 → PATH 中的 python
#   2. 用 https://pypi.org/simple/ 做 TLS 自检（旧 OpenSSL 会在这里被淘汰并给出明确原因）
#   3. 在 %USERPROFILE%\.dsh\runtimes\dsh-ssh-otp 建 venv 并安装 paramiko
#
# 想装到别处：先设置 DSH_SSH_OTP_VENV 再运行。

$ErrorActionPreference = 'Stop'

$venv = $env:DSH_SSH_OTP_VENV
if ([string]::IsNullOrWhiteSpace($venv)) {
    $venv = Join-Path $HOME '.dsh\runtimes\dsh-ssh-otp'
}

function Get-PythonInfo($exe) {
    $info = & $exe -c "import sys,ssl; print(sys.version.split()[0]); print(ssl.OPENSSL_VERSION)" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $info) { return $null }
    $lines = @($info)
    return @{ Version = $lines[0]; OpenSSL = if ($lines.Count -gt 1) { $lines[1] } else { 'unknown' } }
}

function Test-PypiTls($exe) {
    & $exe -c "import ssl,urllib.request; urllib.request.urlopen('https://pypi.org/simple/', timeout=20)" 2>$null
    return $LASTEXITCODE -eq 0
}

# ---- 候选解释器 ---------------------------------------------------------
$candidates = @()

$explicit = $env:DSH_SSH_OTP_BASE_PYTHON
if (-not [string]::IsNullOrWhiteSpace($explicit)) {
    $candidates += $explicit
}

$runtimeRoot = Join-Path $HOME '.dsh\dsh-runtimes'
if (Test-Path $runtimeRoot) {
    Get-ChildItem -Path $runtimeRoot -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        $p = Join-Path $_.FullName 'dependencies\python\python.exe'
        if (Test-Path $p) { $candidates += $p }
    }
}

$pathPython = Get-Command python -ErrorAction SilentlyContinue
if ($pathPython) { $candidates += $pathPython.Source }

$candidates = $candidates | Select-Object -Unique
if ($candidates.Count -eq 0) {
    Write-Error "找不到任何 Python 解释器。请安装 Python 3.11+（勾选 Add python.exe to PATH），或使用 DSH 自带运行时后重试。"
}

# ---- TLS 自检 -----------------------------------------------------------
$base = $null
$rejected = @()
foreach ($py in $candidates) {
    if (-not (Test-Path $py)) { continue }
    $info = Get-PythonInfo $py
    $label = if ($info) { "Python $($info.Version) / $($info.OpenSSL)" } else { '无法执行' }
    if (-not $info) {
        $rejected += "$py（$label）"
        continue
    }
    if (Test-PypiTls $py) {
        $base = $py
        Write-Host "使用解释器：$py（$label，PyPI TLS 正常）"
        break
    }
    $rejected += "$py（$label：访问 pypi.org 时 TLS 握手失败）"
}

if (-not $base) {
    $detail = if ($rejected.Count -gt 0) { "`n已尝试：`n  " + ($rejected -join "`n  ") } else { '' }
    Write-Error ("找不到能正常访问 PyPI 的 Python 解释器：旧版本（如 3.10.0 / OpenSSL 1.1.1）会在 TLS 握手阶段失败。" +
        "`n请安装 Python 3.11+，或改用 DSH 自带运行时（~/.dsh/dsh-runtimes/*/dependencies/python/python.exe）后重试。" + $detail)
}

# ---- 建 venv 并安装 paramiko -------------------------------------------
$venvPython = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Host "创建虚拟环境：$venv"
    & $base -m venv $venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        Write-Error "创建虚拟环境失败：$venv。请确认目标目录可写，且解释器 $base 完整。"
    }
}

Write-Host "安装 paramiko …"
& $venvPython -m pip install --upgrade pip --disable-pip-version-check --timeout 30 --retries 2
if ($LASTEXITCODE -ne 0) {
    Write-Warning "升级 pip 失败，继续用现有 pip 安装 paramiko。"
}
& $venvPython -m pip install --upgrade paramiko --disable-pip-version-check --timeout 30 --retries 2
if ($LASTEXITCODE -ne 0) {
    Write-Error ("安装 paramiko 失败。若本机需要代理，请先设置 HTTP_PROXY/HTTPS_PROXY 后重试；" +
        "若提示 SSL 错误，请改用 Python 3.11+ 或 DSH 自带运行时。")
}

& $venvPython -c "import paramiko; print('paramiko', paramiko.__version__, 'ok')"
if ($LASTEXITCODE -ne 0) {
    Write-Error "paramiko 安装后仍无法导入，请把上面的输出发给我。"
}

Write-Host ""
Write-Host "完成。插件默认会使用：$venvPython"
