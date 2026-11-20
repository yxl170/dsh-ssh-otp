#!/usr/bin/env bash
# 为 dsh-ssh-otp 准备 Python 运行时（Linux / macOS）
#
# 用法：
#   bash scripts/setup-python.sh
#
# 行为：
#   1. 依次探测候选解释器：DSH_SSH_OTP_BASE_PYTHON → DSH 自带运行时 → PATH 中的 python3/python
#   2. 用 https://pypi.org/simple/ 做 TLS 自检（旧 OpenSSL 会在这里被淘汰并给出明确原因）
#   3. 在 ~/.dsh/runtimes/dsh-ssh-otp 建 venv 并安装 paramiko
#
# 想装到别处：DSH_SSH_OTP_VENV=/your/path bash scripts/setup-python.sh

set -euo pipefail

venv="${DSH_SSH_OTP_VENV:-$HOME/.dsh/runtimes/dsh-ssh-otp}"

candidates=()
if [ -n "${DSH_SSH_OTP_BASE_PYTHON:-}" ]; then
  candidates+=("$DSH_SSH_OTP_BASE_PYTHON")
fi
for managed in "$HOME"/.dsh/dsh-runtimes/*/dependencies/python/bin/python3; do
  [ -x "$managed" ] && candidates+=("$managed")
done
command -v python3 >/dev/null 2>&1 && candidates+=("$(command -v python3)")
command -v python >/dev/null 2>&1 && candidates+=("$(command -v python)")

if [ "${#candidates[@]}" -eq 0 ]; then
  echo "找不到任何 Python 解释器。请安装 Python 3.11+ 后重试。" >&2
  exit 1
fi

base=""
rejected=()
for py in "${candidates[@]}"; do
  [ -x "$py" ] || continue
  info="$("$py" -c "import sys,ssl;print(sys.version.split()[0], ssl.OPENSSL_VERSION)" 2>/dev/null || true)"
  if [ -z "$info" ]; then
    rejected+=("$py（无法执行）")
    continue
  fi
  if "$py" -c "import ssl,urllib.request;urllib.request.urlopen('https://pypi.org/simple/',timeout=20)" >/dev/null 2>&1; then
    base="$py"
    echo "使用解释器：$py（$info，PyPI TLS 正常）"
    break
  fi
  rejected+=("$py（$info：访问 pypi.org 时 TLS 握手失败）")
done

if [ -z "$base" ]; then
  {
    echo "找不到能正常访问 PyPI 的 Python 解释器：旧版本（如 3.10.0 / OpenSSL 1.1.1）会在 TLS 握手阶段失败。"
    echo "请安装 Python 3.11+，或改用 DSH 自带运行时（~/.dsh/dsh-runtimes/*/dependencies/python/bin/python3）后重试。"
    if [ "${#rejected[@]}" -gt 0 ]; then
      echo "已尝试："
      printf '  %s\n' "${rejected[@]}"
    fi
  } >&2
  exit 1
fi

python_bin="$venv/bin/python"
if [ ! -x "$python_bin" ]; then
  echo "创建虚拟环境：$venv"
  if ! "$base" -m venv "$venv" || [ ! -x "$python_bin" ]; then
    echo "创建虚拟环境失败：$venv。请确认目标目录可写，且解释器 $base 完整。" >&2
    exit 1
  fi
fi

echo "安装 paramiko …"
"$python_bin" -m pip install --upgrade pip --disable-pip-version-check --timeout 30 --retries 2 \
  || echo "升级 pip 失败，继续用现有 pip 安装 paramiko。" >&2
"$python_bin" -m pip install --upgrade paramiko --disable-pip-version-check --timeout 30 --retries 2

"$python_bin" -c "import paramiko; print('paramiko', paramiko.__version__, 'ok')"
echo
echo "完成。若插件不在默认位置，请把 ~/.dsh/dsh-ssh-otp.json 的 pythonPath 指到：$venv/bin/python"
