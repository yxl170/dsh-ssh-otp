# dsh-ssh-otp

SSH tools for [DeepSeek Harness](https://github.com/deepseek-ai) (DSH) that can complete a **password + TOTP two-factor login non-interactively**.

This exists because some HPC clusters disable public key authentication (`PubkeyAuthentication no`) and force `keyboard-interactive` with a time-based one-time password on every new connection. Neither the stock `ssh_exec` style plugins nor a plain `ssh user@host` call can satisfy that prompt from an automated agent.

## What it gives you

Four agent tools:

| Tool | Purpose |
| --- | --- |
| `ssh_otp_hosts` | List configured hosts and whether their credentials are present in the host credential store |
| `ssh_otp_exec` | Run a command, return `exitCode` / `stdout` / `stderr` |
| `ssh_otp_upload` | SFTP upload, refuses to overwrite unless `overwrite=true` |
| `ssh_otp_download` | SFTP download, refuses to overwrite unless `overwrite=true` |

Plus a settings page under **Settings → Plugins → dsh-ssh-otp** that adds, edits and deletes hosts, stores the two secrets, tests a connection, and edits the default timeout. Only hosts and the timeout are writable there; `pythonPath`/`servicePath` stay file-only so the page cannot become a way to run an arbitrary interpreter. Saving writes `~/.dsh/dsh-ssh-otp.json` atomically (previous version kept as `.bak`); secrets still go only to the credential store.

Behaviour worth knowing:

- **One long-lived connection per host.** Commands on the same host are serialized; different hosts run in parallel. Connections idle out after 10 minutes (configurable).
- **No automatic auth retry.** A rejected login is reported after exactly one attempt, because most failures are a wrong username or password and a retry only doubles the fail2ban counter. Set `"retryOnAuthFailure": true` in `~/.dsh/dsh-ssh-otp.json` if you really want one extra attempt in the next TOTP window (only when a code prompt was involved). Two consecutive failures make the plugin stop connecting for 10 minutes instead of hammering the server.
- **Output is bounded honestly.** Anything over 64 KiB is spilled to disk and the result reports `truncated: true` plus a path holding the complete stream (`~/.dsh/ssh-otp-spill/`, or the service temp spool for very large streams). Earlier builds could stop reading while a fast producer was still writing; the read loop now waits for channel EOF before it reports totals.
- **Secrets never touch disk here.** They are read from the DSH credential store at call time and handed to the Python child process over its stdin pipe.

## Architecture

```
DSH host (Node)                         child process (Python)
┌───────────────────────────┐           ┌────────────────────────────┐
│ tools: ssh_otp_*          │  JSON     │ ssh_service.py             │
│ credentials.resolve(ref)  │ ────────► │  paramiko Transport        │
│ → password / TOTP seed    │  stdio    │  keyboard-interactive OTP  │
│ web routes (loopback)     │ ◄──────── │  one connection per alias  │
└───────────────────────────┘           └────────────────────────────┘
```

The Node half never speaks SSH. The Python half never knows where secrets came from — it receives them per call and keeps them only in memory.

## Requirements

- DSH `>= 0.2.2-rc.3`
- Node `>= 24`
- Python `>= 3.10` with `paramiko` — prefer **3.11+**. Python 3.10.0 ships OpenSSL 1.1.1, whose TLS stack cannot reach PyPI, so `pip install paramiko` fails during setup; the setup script detects this and tells you instead of dumping an SSL traceback.

## Install

The plugin is a plain npm package with no build step, so it installs straight from this repository.

**1. Prepare Python once**

Create the virtualenv the plugin expects by default and install `paramiko` — no clone required:

```powershell
# Windows (PowerShell)
$s = "$env:TEMP\dsh-ssh-otp-setup.ps1"
irm https://raw.githubusercontent.com/yxl170/dsh-ssh-otp/main/scripts/setup-python.ps1 -OutFile $s
powershell -ExecutionPolicy Bypass -File $s
```

```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/yxl170/dsh-ssh-otp/main/scripts/setup-python.sh | bash
```

The Windows script creates `%USERPROFILE%/.dsh/runtimes/dsh-ssh-otp/Scripts/python.exe`; the Unix one creates `~/.dsh/runtimes/dsh-ssh-otp/bin/python`. The script probes candidate interpreters (including DSH's bundled runtime under `~/.dsh/dsh-runtimes/*/dependencies/python/`) and checks PyPI TLS before building the venv, so an old interpreter fails with a readable reason instead of an OpenSSL traceback.

You can also skip the script and point the plugin at any interpreter that already has `paramiko`; see **Troubleshooting** below.

**2. Add the plugin in DSH**

**Settings → Plugins → Add plugin**, paste:

```
https://github.com/yxl170/dsh-ssh-otp
```

(`github:yxl170/dsh-ssh-otp` works too.) Install, then enable the row. No restart is needed for a fresh install.

**3. Configure it**

Open **Settings → Plugins → dsh-ssh-otp**:

1. **新增主机** — alias, host, port, user, auth kind; credential names are derived from the alias when left empty.
2. **保存主机**, then paste the password (and TOTP seed) into the **凭据** rows that appear and save them.
3. **测试连接** to confirm the login works.

The page writes `~/.dsh/dsh-ssh-otp.json` atomically and keeps the previous copy as `.bak`. Secrets never go into that file — they are stored in the DSH credential store.

<details>
<summary>Manual install (development checkout)</summary>

Link a local checkout into the profile instead of installing from GitHub:

```jsonc
// ~/.dsh/profiles/<profile>/package.json
{
  "dependencies": { "dsh-ssh-otp": "link:C:/path/to/dsh-ssh-otp" },
  "dsh": { "profile": { "bundles": ["...", "dsh-ssh-otp"] } }
}
```

Then make the package resolvable under the profile's `node_modules` (a junction/symlink is enough) and restart DSH — the bundle list is read at startup only.

</details>

## Configure hosts

Easiest path: add a host on the settings page — alias, address, port, user, auth kind, and the two credential names (leave them empty and they are derived from the alias). The page writes the config for you.

To do it by hand, copy `config.example.json` to `~/.dsh/dsh-ssh-otp.json` and edit:

```jsonc
{
  "pythonPath": "~/.dsh/runtimes/dsh-ssh-otp/Scripts/python.exe",
  "servicePath": "~/.dsh/plugins/dsh-ssh-otp/server/ssh_service.py",
  "idleMinutes": 10,
  "timeoutMs": 120000,
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

`kind` is either `password_totp` or `password`. Note that the SSH login name is often **not** the same as the account name reported by `whoami` after login — use whatever you type into `ssh`.

## Store the secrets

Preferred: open **Settings → Plugins → dsh-ssh-otp** and paste the values. They go straight into the DSH credential store.

Manual fallback — append to `~/.dsh/.credentials.yaml`:

```yaml
version: 1
refs:
  CLUSTER_A_SSH_PASSWORD: "…password…"
  CLUSTER_A_TOTP_SEED: "JBSWY3DPEHPK3PXP"
```

Two traps, both verified against the credential store implementation:

1. **Quote every value.** An unquoted numeric password is parsed as a YAML number and rejected.
2. **Validation is all-or-nothing.** A single invalid entry rejects the whole file, which also invalidates every unrelated credential in it.

## Troubleshooting

**“找不到可用的 Python 解释器”** — the plugin probes, in order: `DSH_SSH_OTP_PYTHON`, `pythonPath` from the config, DSH's bundled runtime, the default venv, then `python3`/`python` on `PATH`. The error lists every candidate it tried and prints the setup command for your platform. Run that command once, or set `pythonPath` to an interpreter that can `import paramiko`.

**“疑似触发服务器 fail2ban”** — two consecutive auth failures within 10 minutes trip a local breaker; the plugin refuses to connect for 10 minutes instead of extending the ban. Check the username (the SSH login name, not necessarily what `whoami` prints later), the password, and the TOTP seed. The error from the failed attempt also shows the server's prompt sequence, e.g. `[Verification code:→TOTP, Password:→PASSWORD]`; a single `Password` round usually means the username or password is wrong.

**Deliberate escape hatch** — `"retryOnAuthFailure": true` in `~/.dsh/dsh-ssh-otp.json` allows one extra attempt after a rejected TOTP code. Only enable it when you accept the extra failed attempt in the server's counter.

## Security model

Be honest with yourself about what this plugin is: **storing the TOTP seed together with the password collapses two-factor auth into one factor.** The seed is the master key — anyone who reads it can mint valid codes forever.

What the plugin does to reduce exposure:

- Secrets live in the DSH credential store, not in this plugin's config and not in its source.
- They are resolved per call and passed over an in-process pipe; never written, never logged, never echoed in errors.
- The settings page routes are **loopback-only** and additionally verify the `Host` header.
- Credential writes are restricted to ref names that actually appear in the host config.

What it cannot protect you against:

- Anything that can read your user's credential store.
- Malware on the machine holding the seed.

If the seed leaks, rotate it on the server (for a typical Google-Authenticator setup: regenerate the key) and update the stored ref.

## Limitations

- **No TTY.** `ssh_otp_exec` runs commands without a pseudo-terminal, so `top`, `vi`, and interactive `salloc` shells do not work. Use a normal terminal client for those.
- **No login shell.** Commands run through the server's non-login shell, so an interactive `~/.bashrc` is not sourced and site module environments may be absent (for example `nvidia-smi` can be missing from `PATH`); call tools by absolute path or `source` the environment explicitly.
- **A timeout stops waiting, not the remote process.** `timeoutMs` returns `timedOut: true` and closes the channel; a long build or `sleep` may keep running on the server. Commands on one host are serialized by design.
- The TOTP path assumes 6 digits / 30 seconds / SHA-1 (Google Authenticator defaults).
- Windows is the primary target (the plugin was developed there); the Python service is platform-neutral.
- Verified against OpenSSH 7.4 with `keyboard-interactive` split across two prompt rounds. Servers that ask more rounds, or that combine prompts, should still work — the handler answers per prompt, not per round.

## Why not just patch existing SSH plugins?

The common Node implementation answers `keyboard-interactive` with the password only when **every** prompt matches `/password/i`, and otherwise fails the handshake. A server that asks `Verification code:` first never matches, so authentication cannot proceed. This plugin sidesteps the whole question by delegating SSH to paramiko, which exposes prompt lists directly.

## License

MIT

---

## 中文摘要

这个插件给 DSH 加上能**自动完成「密码 + 动态验证码」二次验证登录**的 SSH 工具，用于那些禁用了公钥认证、每次连接都要求 `keyboard-interactive` 验证码的集群。

提供 `ssh_otp_hosts` / `ssh_otp_exec` / `ssh_otp_upload` / `ssh_otp_download` 四个工具，外加一个设置页：增删改主机、录入密钥、测试连接、修改默认超时（解释器与服务脚本路径只读）。

**安全前提必须清楚**：把密码和 TOTP 种子都存在同一台机器上，等于把双因子降成单因子。种子就是母本，谁拿到它就能一直生成有效验证码。插件能做的是"不再把明文写进配置、日志和报错里"，做不到"泄露后仍然安全"。种子一旦外泄，请立刻在服务器上重置二次验证密钥。
