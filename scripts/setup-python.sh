#!/usr/bin/env bash
# 为 dsh-ssh-otp 准备 Python 运行时（Linux / macOS）
#
# 用法：
#   bash scripts/setup-python.sh
#
# 默认创建在 ~/.dsh/runtimes/dsh-ssh-otp（与插件默认配置一致）：
#   ~/.dsh/runtimes/dsh-ssh-otp/bin/python
#
# 想装到别处：DSH_SSH_OTP_VENV=/your/path bash scripts/setup-python.sh

set -euo pipefail

venv="${DSH_SSH_OTP_VENV:-$HOME/.dsh/runtimes/dsh-ssh-otp}"

if command -v python3 >/dev/null 2>&1; then
  py=python3
elif command -v python >/dev/null 2>&1; then
  py=python
else
  echo "找不到 python3/python，请先安装 Python 3.10+" >&2
  exit 1
fi

if [ ! -x "$venv/bin/python" ]; then
  echo "创建虚拟环境：$venv"
  "$py" -m venv "$venv"
fi

echo "安装 paramiko …"
"$venv/bin/python" -m pip install --upgrade pip >/dev/null
"$venv/bin/python" -m pip install --upgrade paramiko

"$venv/bin/python" -c "import paramiko; print('paramiko', paramiko.__version__, 'ok')"
echo
echo "完成。若插件不在默认位置，请把 ~/.dsh/dsh-ssh-otp.json 的 pythonPath 指到：$venv/bin/python"
