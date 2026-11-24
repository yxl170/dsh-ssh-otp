"""Standalone runtime for the Antigravity/MCP entry point.

The DSH plugin owns protocol/tool registration. This module deliberately knows
nothing about DSH: it reads local config/secrets and reuses the proven paramiko
Connection implementation from server/ssh_service.py.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import socket
import ssl
import stat
import sys
import threading
import time
from pathlib import Path

import paramiko

CONFIG_ENV = 'SSH_OTP_CONFIG'
SECRETS_ENV = 'SSH_OTP_SECRETS'
SECRET_ENV_PREFIX = 'SSH_OTP_SECRET_'
DEFAULT_TIMEOUT_MS = 120000
MAX_TIMEOUT_MS = 3_600_000
INLINE_MAX_BYTES = 64 * 1024
INLINE_HEAD_CHARS = 32 * 1024
AUTH_FAILURE_WINDOW_SECONDS = 10 * 60
AUTH_FAILURE_ATTEMPT_LIMIT = 2
ALIAS_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
REF_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')


class McpToolError(Exception):
    """An error safe to show to the MCP caller. Never includes secret values."""


def default_config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV, str(Path.home() / '.ssh-otp' / 'config.json'))).expanduser()


def default_secrets_path() -> Path:
    return Path(os.environ.get(SECRETS_ENV, str(Path.home() / '.ssh-otp' / 'secrets.json'))).expanduser()


def load_ssh_service():
    """Import the DSH service module by path without running its stdio main loop."""
    source = Path(__file__).resolve().parents[1] / 'server' / 'ssh_service.py'
    spec = importlib.util.spec_from_file_location('_dsh_ssh_otp_service', source)
    if spec is None or spec.loader is None:
        raise McpToolError(f'找不到 SSH 服务模块：{source}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _absolute_local_path(value: str, label: str) -> str:
    path = Path(str(value or '')).expanduser()
    if not path.is_absolute():
        raise McpToolError(f'{label}必须是本机绝对路径，避免把文件写到错误的位置：{value}')
    return str(path)


def _shape_stream(text: str, label: str, spill_from_service: str | None, total_bytes: int | None):
    text = text or ''
    total = int(total_bytes if total_bytes is not None else len(text.encode('utf-8')))
    spill_path = spill_from_service
    if spill_path is None and total > INLINE_MAX_BYTES:
        spill_dir = Path.home() / '.ssh-otp' / 'spill'
        spill_dir.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt':
            os.chmod(spill_dir, 0o700)
        candidate = spill_dir / f'{label}-{time.time_ns()}.log'
        candidate.write_text(text, encoding='utf-8')
        if os.name != 'nt':
            os.chmod(candidate, 0o600)
        spill_path = str(candidate)
    if spill_path is None:
        return {'text': text, 'truncated': False, 'spillPath': None}
    head = text[:INLINE_HEAD_CHARS]
    return {
        'text': f'{head}\n…[output truncated: {total} bytes total, full output at {spill_path}]',
        'truncated': True,
        'spillPath': spill_path,
    }


class SshOtpRuntime:
    _reaper_started = False

    def __init__(self, config_path: str | None = None, secrets_path: str | None = None, service=None):
        self.config_path = Path(config_path).expanduser() if config_path else default_config_path()
        self.secrets_path = Path(secrets_path).expanduser() if secrets_path else default_secrets_path()
        self.service = service or load_ssh_service()
        self.auth_failures: dict[str, dict[str, float | int]] = {}
        if not SshOtpRuntime._reaper_started:
            threading.Thread(target=self.service.reaper, daemon=True, name='ssh-otp-idle-reaper').start()
            SshOtpRuntime._reaper_started = True

    # ------------------------------------------------------------- config/secrets
    def load_config(self) -> dict:
        config = {
            'timeoutMs': DEFAULT_TIMEOUT_MS,
            'retryOnAuthFailure': False,
            'hosts': {},
        }
        if not self.config_path.exists():
            return config
        try:
            parsed = json.loads(self.config_path.read_text(encoding='utf-8-sig'))
        except Exception as error:
            raise McpToolError(f'主机配置解析失败：{self.config_path}：{error}')
        if not isinstance(parsed, dict):
            raise McpToolError(f'主机配置必须是 JSON 对象：{self.config_path}')
        config.update(parsed)
        hosts = config.get('hosts')
        if not isinstance(hosts, dict):
            raise McpToolError('配置中的 hosts 必须是对象（alias -> 主机）')
        timeout = config.get('timeoutMs', DEFAULT_TIMEOUT_MS)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1000 <= timeout <= MAX_TIMEOUT_MS:
            raise McpToolError(f'timeoutMs 必须是 1000–{MAX_TIMEOUT_MS} 的整数')
        for alias, host in hosts.items():
            self._validate_host(alias, host)
        return config

    @staticmethod
    def _validate_host(alias: str, host: dict) -> None:
        if not ALIAS_RE.fullmatch(alias):
            raise McpToolError(f'主机别名不合法：{alias}')
        if not isinstance(host, dict):
            raise McpToolError(f'{alias}：主机配置必须是对象')
        address = str(host.get('host') or '').strip()
        user = str(host.get('user') or '').strip()
        if not address or re.search(r'\s', address):
            raise McpToolError(f'{alias}：主机地址不能为空，也不能包含空白')
        if not user or re.search(r'\s', user):
            raise McpToolError(f'{alias}：用户名不能为空，也不能包含空白')
        port = host.get('port', 22)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise McpToolError(f'{alias}：端口必须是 1–65535 的整数')
        auth = host.get('auth') or {}
        kind = auth.get('kind', 'password')
        if kind not in ('password', 'password_totp'):
            raise McpToolError(f'{alias}：auth.kind 必须是 password 或 password_totp')
        password_ref = auth.get('passwordRef')
        if not isinstance(password_ref, str) or not REF_RE.fullmatch(password_ref):
            raise McpToolError(f'{alias}：passwordRef 不是合法的凭据名')
        if kind == 'password_totp':
            totp_ref = auth.get('totpRef')
            if not isinstance(totp_ref, str) or not REF_RE.fullmatch(totp_ref):
                raise McpToolError(f'{alias}：二次验证主机必须配置合法的 totpRef')
            if totp_ref == password_ref:
                raise McpToolError(f'{alias}：密码与 TOTP 不能共用同一个凭据名')

    def load_secrets(self) -> dict[str, str]:
        if not self.secrets_path.exists():
            return {}
        try:
            parsed = json.loads(self.secrets_path.read_text(encoding='utf-8-sig'))
        except Exception as error:
            raise McpToolError(f'凭据文件解析失败：{self.secrets_path}：{error}')
        if not isinstance(parsed, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in parsed.items()):
            raise McpToolError(f'凭据文件必须是「凭据名 -> 字符串值」的 JSON 对象：{self.secrets_path}')
        return parsed

    def _secret(self, ref: str, secrets: dict[str, str]) -> str:
        value = os.environ.get(f'{SECRET_ENV_PREFIX}{ref}')
        if value:
            return value
        return secrets.get(ref, '')

    # ------------------------------------------------------------- hosts/doctor
    def hosts(self, _args=None) -> dict:
        config = self.load_config()
        secrets = self.load_secrets()
        rows = []
        for alias, host in config['hosts'].items():
            auth = host['auth']
            kind = auth.get('kind', 'password')
            rows.append({
                'alias': alias,
                'target': f"{host['user']}@{host['host']}:{host.get('port', 22)}",
                'authKind': kind,
                'passwordReady': bool(self._secret(auth['passwordRef'], secrets)),
                'totpReady': bool(self._secret(auth.get('totpRef', ''), secrets)) if kind == 'password_totp' else False,
            })
        return {'hosts': rows, 'configPath': str(self.config_path), 'secretsPath': str(self.secrets_path)}

    def doctor(self, _args=None) -> dict:
        result = {
            'configPath': str(self.config_path),
            'configExists': self.config_path.exists(),
            'secretsPath': str(self.secrets_path),
            'secretsExists': self.secrets_path.exists(),
            'python': sys.version.split()[0],
            'openssl': ssl.OPENSSL_VERSION,
            'paramiko': paramiko.__version__,
            'warnings': [],
            'hosts': [],
        }
        try:
            config = self.load_config()
        except McpToolError as error:
            result['configError'] = str(error)
            return result
        try:
            secrets = self.load_secrets()
            result['secretCount'] = len(secrets)
        except McpToolError as error:
            result['secretsError'] = str(error)
            return result
        if self.secrets_path.exists() and os.name != 'nt':
            mode = stat.S_IMODE(self.secrets_path.stat().st_mode)
            result['secretFileMode'] = oct(mode)
            if mode & 0o077:
                result['warnings'].append(f'凭据文件权限过宽（{oct(mode)}），请执行 chmod 600 {self.secrets_path}')
        if os.name == 'nt':
            result['secretFileMode'] = None
        for alias, host in config['hosts'].items():
            auth = host['auth']
            kind = auth.get('kind', 'password')
            item = {
                'alias': alias,
                'target': f"{host['user']}@{host['host']}:{host.get('port', 22)}",
                'passwordReady': bool(self._secret(auth['passwordRef'], secrets)),
                'totpReady': bool(self._secret(auth.get('totpRef', ''), secrets)) if kind == 'password_totp' else False,
            }
            try:
                with socket.create_connection((host['host'], host.get('port', 22)), timeout=3):
                    item['tcp'] = {'ok': True}
            except OSError as error:
                item['tcp'] = {'ok': False, 'error': str(error)}
            result['hosts'].append(item)
        if not config['hosts']:
            result['warnings'].append(f'尚未配置主机，请编辑 {self.config_path}')
        return result

    # ------------------------------------------------------------- auth gate/connection
    def _assert_auth_allowed(self, alias: str) -> None:
        record = self.auth_failures.get(alias)
        if record is None:
            return
        if time.time() - float(record['at']) > AUTH_FAILURE_WINDOW_SECONDS:
            self.auth_failures.pop(alias, None)
            return
        if int(record['attempts']) >= AUTH_FAILURE_ATTEMPT_LIMIT:
            raise McpToolError(
                f'疑似触发服务器 fail2ban：主机 {alias} 已连续 {record["attempts"]} 次认证失败，'
                '请等待约 10 分钟，并先核对用户名、密码与 TOTP seed。'
            )

    def _note_auth_failure(self, alias: str, attempts: int) -> None:
        record = self.auth_failures.get(alias)
        fresh = record is not None and time.time() - float(record['at']) <= AUTH_FAILURE_WINDOW_SECONDS
        self.auth_failures[alias] = {
            'attempts': max(1, attempts) + (int(record['attempts']) if fresh else 0),
            'at': time.time(),
        }

    def _connection_spec(self, alias: str) -> dict:
        config = self.load_config()
        host = config['hosts'].get(alias)
        if host is None:
            known = ', '.join(config['hosts']) or '（无）'
            raise McpToolError(f'未配置主机 "{alias}"。已配置：{known}；请编辑 {self.config_path}')
        auth = host['auth']
        kind = auth.get('kind', 'password')
        secrets = self.load_secrets()
        password = self._secret(auth['passwordRef'], secrets)
        if not password:
            raise McpToolError(
                f'凭据 "{auth["passwordRef"]}" 未设置。请在 {self.secrets_path} 添加该字段，'
                f'或设置环境变量 {SECRET_ENV_PREFIX}{auth["passwordRef"]}。'
            )
        conn_auth = {'kind': kind, 'password': password}
        if kind == 'password_totp':
            seed = self._secret(auth['totpRef'], secrets)
            if not seed:
                raise McpToolError(
                    f'凭据 "{auth["totpRef"]}"（TOTP 种子）未设置。请在 {self.secrets_path} 添加该字段，'
                    f'或设置环境变量 {SECRET_ENV_PREFIX}{auth["totpRef"]}。'
                )
            conn_auth['totp_seed'] = seed
        return {
            'host': host['host'],
            'port': host.get('port', 22),
            'user': host['user'],
            'auth': conn_auth,
        }

    def _request(self, alias: str, operation):
        self._assert_auth_allowed(alias)
        config = self.load_config()
        spec = self._connection_spec(alias)
        try:
            conn = self.service.get_connection(alias, spec)
            conn.retry_on_auth_failure = config.get('retryOnAuthFailure') is True
            result = operation(conn)
        except self.service.AuthFailed as error:
            self._note_auth_failure(alias, error.attempts)
            raise McpToolError(str(error)) from error
        self.auth_failures.pop(alias, None)
        return result

    # ------------------------------------------------------------- tools
    def execute(self, args: dict) -> dict:
        alias = args['host']
        timeout = args.get('timeoutMs') or self.load_config()['timeoutMs']

        def run(conn):
            return conn.exec(args['command'], timeout)

        payload = self._request(alias, run)
        out = _shape_stream(payload.get('stdout', ''), f'{alias}-stdout', payload.get('stdoutSpillPath'), payload.get('stdoutBytes'))
        err = _shape_stream(payload.get('stderr', ''), f'{alias}-stderr', payload.get('stderrSpillPath'), payload.get('stderrBytes'))
        result = {
            'exitCode': payload.get('exitCode', -1),
            'stdout': out['text'],
            'stderr': err['text'],
            'truncated': out['truncated'] or err['truncated'],
            'timedOut': payload.get('timedOut') is True,
        }
        spill = out['spillPath'] or err['spillPath']
        if spill:
            result['spillPath'] = spill
        if isinstance(payload.get('stdoutBytes'), int):
            result['bytesTotal'] = payload['stdoutBytes']
        return result

    def upload(self, args: dict) -> dict:
        local = _absolute_local_path(args['localPath'], 'localPath')
        if not Path(local).is_file():
            raise McpToolError(f'本机文件不存在：{local}')

        def run(conn):
            return conn.upload(local, args['remotePath'], args.get('overwrite') is True)

        try:
            return self._request(args['host'], run)
        except FileExistsError as error:
            raise McpToolError(str(error)) from error

    def download(self, args: dict) -> dict:
        local = _absolute_local_path(args['localPath'], 'localPath')

        def run(conn):
            return conn.download(args['remotePath'], local, args.get('overwrite') is True)

        try:
            return self._request(args['host'], run)
        except (FileExistsError, FileNotFoundError) as error:
            raise McpToolError(str(error)) from error
