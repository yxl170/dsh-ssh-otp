# 在 Antigravity 中使用（MCP 版）

这是一个独立的 **MCP stdio server**：让 Antigravity 通过 `ssh_otp_*` 工具完成「密码 + TOTP 动态验证码」登录、远程执行和 SFTP 传输。它复用本仓库 `server/ssh_service.py` 的 SSH 核心，但**不依赖 DeepSeek Harness**，不会改变现有 DSH 插件。

首版只支持本机 stdio，不提供远程 HTTP/SSE 服务，也不需要 Node 或 MCP SDK。

## 1. 准备

要求：Python 3.10+（建议 3.11+）和 `paramiko`。不要在本机 PATH 随便找一个旧 Python 就直接装：旧 OpenSSL 可能无法访问 PyPI，安装脚本会先检查。

```bash
git clone https://github.com/yxl170/dsh-ssh-otp.git
cd dsh-ssh-otp
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-mcp.ps1
```

Linux / macOS：

```bash
bash scripts/setup-mcp.sh
```

脚本会：

- 创建 `~/.ssh-otp/venv` 并安装 `paramiko`；
- 在配置不存在时创建 `~/.ssh-otp/config.json` 与 `~/.ssh-otp/secrets.json`，**不会覆盖已有文件**；
- 将 secrets 文件限制为当前用户可读（Windows ACL / POSIX `chmod 600`）。

如果安装失败，脚本会说明是解释器、PyPI TLS、venv 还是 pip 的问题，而不是只给一段 SSL 堆栈。

## 2. 配置主机与凭据

编辑 `~/.ssh-otp/config.json`。可直接从 [examples/config.example.json](examples/config.example.json) 复制：

```json
{
  "timeoutMs": 120000,
  "retryOnAuthFailure": false,
  "hosts": {
    "cluster-a": {
      "host": "cluster.example.org",
      "port": 22,
      "user": "your-username",
      "auth": {
        "kind": "password_totp",
        "passwordRef": "CLUSTER_A_SSH_PASSWORD",
        "totpRef": "CLUSTER_A_TOTP_SEED"
      }
    }
  }
}
```

编辑 `~/.ssh-otp/secrets.json`，key 必须与上面的 ref 完全一致：

```json
{
  "CLUSTER_A_SSH_PASSWORD": "<password>",
  "CLUSTER_A_TOTP_SEED": "<base32-totp-seed>"
}
```

注意：

- SSH 登录名以你平时 `ssh` 时使用的名称为准，不一定是登录后 `whoami` 显示的名称；
- `retryOnAuthFailure` 默认必须为 `false`。只有明确接受额外失败计数时才开启；即使开启，没有 TOTP 轮次的失败也不会重试；
- 秘密文件不要放进仓库，不要贴进 Antigravity 的 `mcp_config.json`，也不要发给模型；
- 单个 ref 可用环境变量 `SSH_OTP_SECRET_<REF>` 临时覆盖，例如 `SSH_OTP_SECRET_CLUSTER_A_SSH_PASSWORD`。优先级高于 secrets 文件。

**已有 DSH 配置**：可把 `SSH_OTP_CONFIG` 指向 `~/.dsh/dsh-ssh-otp.json`，主机部分可直接复用；但 secrets 仍需在 `~/.ssh-otp/secrets.json` 中按同名 ref 配置，MCP 不会读取 DSH 凭据库。

## 3. 配置 Antigravity

将 [examples/mcp_config.windows.json](examples/mcp_config.windows.json)（或 [posix](examples/mcp_config.posix.json)）的 `mcpServers` 内容合并到 `~/.gemini/config/mcp_config.json`，把路径改成实际路径：

```json
{
  "mcpServers": {
    "ssh-otp": {
      "command": "C:/Users/you/.ssh-otp/venv/Scripts/python.exe",
      "args": ["C:/src/dsh-ssh-otp/mcp/ssh_otp_mcp.py"],
      "env": {
        "SSH_OTP_CONFIG": "C:/Users/you/.ssh-otp/config.json",
        "SSH_OTP_SECRETS": "C:/Users/you/.ssh-otp/secrets.json"
      }
    }
  }
}
```

在 Antigravity 中打开 **Settings → Customizations → Installed MCP servers**，刷新，确认 `ssh-otp` 出现且工具可用。CLI 用户可用 `agy` 启动后通过 `/mcp` 检查。

先让它执行：

```text
调用 ssh_otp_doctor，检查 SSH 配置。
```

`doctor` 只检查配置、secrets 权限、Python/paramiko 和 TCP 可达性，**不会真实登录，也不会消耗验证码**。然后再执行：

```text
用 ssh_otp_exec 在 cluster-a 上运行 hostname; whoami。
```

## 4. 工具与返回约定

| 工具 | 必填参数 | 说明 |
| --- | --- | --- |
| `ssh_otp_hosts` | 无 | 主机和凭据就绪状态，不发起连接 |
| `ssh_otp_exec` | `host`, `command` | 可选 `timeoutMs`；返回退出码、stdout、stderr、超时和截断信息 |
| `ssh_otp_upload` | `host`, `localPath`, `remotePath` | 本机绝对路径；默认拒绝覆盖，显式 `overwrite: true` 才覆盖 |
| `ssh_otp_download` | `host`, `remotePath`, `localPath` | 本机绝对路径；默认拒绝覆盖，失败不留残件 |
| `ssh_otp_doctor` | 无 | 配置、权限、运行时和 TCP 检查，不真实登录 |

行为边界：

- 同一主机连续 2 次认证失败后，本地拦截 10 分钟并提示疑似 fail2ban；成功一次会清零，重启 MCP 也会清零；
- 认证失败包含服务器提示序列（如 `[Verification code:→TOTP, Password:→PASSWORD]`）和尝试次数，不包含任何输入值；
- 超过 64 KiB 的输出只内联头部，返回 `truncated: true`、真实 `bytesTotal` 和完整文件的 `spillPath`；
- `timeoutMs` 到期返回 `timedOut: true`，只停止等待，**不保证杀掉远程进程**；
- 不分配 TTY，`top`、`vi`、交互式 `salloc` 不适用；非登录 shell 不保证加载交互式 `~/.bashrc` 或集群模块环境；
- 工具失败通过 MCP `isError: true` 返回；JSON-RPC 错误只用于协议/参数问题。

## 5. 更新、测试与卸载

更新：

```bash
cd /path/to/dsh-ssh-otp
git pull
# 然后在 Antigravity 中刷新/重启该 MCP server
```

运行测试（不需要真实 SSH）：

```bash
# Windows 示例，Unix 换成 ~/.ssh-otp/venv/bin/python
~/.ssh-otp/venv/Scripts/python.exe -m unittest discover -s tests -p "test_mcp_*.py" -v
```

卸载：从 `~/.gemini/config/mcp_config.json` 删除 `ssh-otp` 项，刷新 Antigravity；再按需删除 clone 与 `~/.ssh-otp/`。删除后者会同时删除本地 secrets，请确认不再需要。

## 首版不做的事

远程 MCP、DPAPI/Keychain、pip/uvx 分发、通过 MCP 修改密码或 TOTP seed。现有 DSH 插件仍走 [上级 README](../README.md) 的安装方式，版本互不影响。
