#!/usr/bin/env bash
# 为 Antigravity / MCP 准备 dsh-ssh-otp 运行时与本地配置（Linux / macOS）
#
#   bash scripts/setup-mcp.sh
#
# 创建：
#   ~/.ssh-otp/venv/bin/python（复用 setup-python.sh 的候选探测和 TLS 自检）
#   ~/.ssh-otp/config.json（不存在才创建）
#   ~/.ssh-otp/secrets.json（不存在才创建为 {}，chmod 600）

set -euo pipefail

root="$HOME/.ssh-otp"
venv="$root/venv"
config="$root/config.json"
secrets="$root/secrets.json"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$root"
chmod 700 "$root"
DSH_SSH_OTP_VENV="$venv" bash "$script_dir/setup-python.sh"

if [ ! -f "$config" ]; then
  cat > "$config" <<'JSON'
{
  "timeoutMs": 120000,
  "retryOnAuthFailure": false,
  "hosts": {}
}
JSON
fi
if [ ! -f "$secrets" ]; then
  printf '{}\n' > "$secrets"
fi
chmod 600 "$secrets"

cat <<EOF

下一步：
  1. 编辑 $config，添加 hosts
  2. 编辑 $secrets，按 passwordRef/totpRef 填入密码与种子
  3. 把 mcp/examples/mcp_config.posix.json 中的路径改成你的实际路径，合并到 ~/.gemini/config/mcp_config.json
  4. 在 Antigravity 的 Installed MCP servers 中刷新，然后调用 ssh_otp_doctor
EOF
