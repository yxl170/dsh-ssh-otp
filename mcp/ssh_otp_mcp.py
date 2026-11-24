#!/usr/bin/env python
"""MCP stdio server exposing dsh-ssh-otp tools to Antigravity and other MCP hosts.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout. No SDK dependency.
Only protocol messages may use stdout; every diagnostic goes to stderr.
"""
from __future__ import annotations

import json
import sys
import traceback

try:
    from runtime import McpToolError, SshOtpRuntime
except ModuleNotFoundError as error:  # pragma: no cover - startup diagnostics
    if error.name == 'paramiko':
        sys.stderr.write(
            '缺少 paramiko。请先运行 scripts/setup-mcp.ps1（Windows）或 '
            'scripts/setup-mcp.sh（Linux/macOS），再用该 venv 启动本服务。\n'
        )
        raise SystemExit(1) from error
    raise

SERVER_NAME = 'dsh-ssh-otp-mcp'
SERVER_VERSION = '0.1.0'
PROTOCOL_VERSIONS = {'2024-11-05', '2025-03-26', '2025-06-18'}
FALLBACK_PROTOCOL_VERSION = '2025-06-18'
MAX_LINE_BYTES = 16 * 1024 * 1024


class InvalidParams(Exception):
    pass


def tool_definitions():
    host = {'type': 'string', 'description': '主机别名（见 ssh_otp_hosts）'}
    return [
        {
            'name': 'ssh_otp_hosts',
            'description': '列出已配置的 SSH 主机，以及密码/TOTP 凭据是否就绪；不发起 SSH 连接。',
            'inputSchema': {'type': 'object', 'additionalProperties': False, 'properties': {}},
        },
        {
            'name': 'ssh_otp_exec',
            'description': '在远程主机执行一条 shell 命令。密码与 TOTP 二次验证由本服务完成；大输出会给出完整落盘路径。',
            'inputSchema': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['host', 'command'],
                'properties': {
                    'host': host,
                    'command': {'type': 'string', 'description': '远程 shell 命令；多条命令用 ; 或 && 连接'},
                    'timeoutMs': {'type': 'integer', 'minimum': 1000, 'maximum': 3600000,
                                  'description': '执行超时（毫秒），默认取配置，通常 120000'},
                },
            },
        },
        {
            'name': 'ssh_otp_upload',
            'description': '把 MCP 所在机器的文件上传到远程主机（SFTP）。默认不覆盖远程同名文件。',
            'inputSchema': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['host', 'localPath', 'remotePath'],
                'properties': {
                    'host': host,
                    'localPath': {'type': 'string', 'description': '本机绝对文件路径'},
                    'remotePath': {'type': 'string', 'description': '远程目标文件路径（不是目录）'},
                    'overwrite': {'type': 'boolean', 'description': '远程已存在时是否覆盖，默认 false'},
                },
            },
        },
        {
            'name': 'ssh_otp_download',
            'description': '把远程文件下载到 MCP 所在机器（SFTP）。默认不覆盖本机同名文件，失败不留残件。',
            'inputSchema': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['host', 'remotePath', 'localPath'],
                'properties': {
                    'host': host,
                    'remotePath': {'type': 'string', 'description': '远程文件路径'},
                    'localPath': {'type': 'string', 'description': '本机绝对目标路径；父目录会自动创建'},
                    'overwrite': {'type': 'boolean', 'description': '本机已存在时是否覆盖，默认 false'},
                },
            },
        },
        {
            'name': 'ssh_otp_doctor',
            'description': '检查配置、凭据权限、paramiko 和主机 TCP 可达性；不真实登录，不消耗 TOTP。',
            'inputSchema': {'type': 'object', 'additionalProperties': False, 'properties': {}},
        },
    ]


def _object(value, label='arguments'):
    if not isinstance(value, dict):
        raise InvalidParams(f'{label} 必须是对象')
    return value


def _allowed(args, fields):
    unknown = sorted(set(args) - set(fields))
    if unknown:
        raise InvalidParams(f'未知参数：{", ".join(unknown)}')


def _text(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidParams(f'缺少非空字符串参数 {key}')
    return value


def _timeout(args):
    value = args.get('timeoutMs')
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or not 1000 <= value <= 3600000:
        raise InvalidParams('timeoutMs 必须是 1000–3600000 的整数')


def _overwrite(args):
    value = args.get('overwrite')
    if value is not None and not isinstance(value, bool):
        raise InvalidParams('overwrite 必须是 boolean')


def validate_call(name, arguments):
    args = _object(arguments)
    if name == 'ssh_otp_hosts':
        _allowed(args, [])
        return 'hosts', args
    if name == 'ssh_otp_doctor':
        _allowed(args, [])
        return 'doctor', args
    if name == 'ssh_otp_exec':
        _allowed(args, ['host', 'command', 'timeoutMs'])
        _text(args, 'host')
        _text(args, 'command')
        _timeout(args)
        return 'execute', args
    if name == 'ssh_otp_upload':
        _allowed(args, ['host', 'localPath', 'remotePath', 'overwrite'])
        for key in ('host', 'localPath', 'remotePath'):
            _text(args, key)
        _overwrite(args)
        return 'upload', args
    if name == 'ssh_otp_download':
        _allowed(args, ['host', 'remotePath', 'localPath', 'overwrite'])
        for key in ('host', 'remotePath', 'localPath'):
            _text(args, key)
        _overwrite(args)
        return 'download', args
    raise InvalidParams(f'未知工具：{name}')


def result_payload(request_id, result):
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def error_payload(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


def tool_content(value, is_error=False):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    return {'content': [{'type': 'text', 'text': text}], 'isError': is_error}


def handle(message, runtime):
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        return error_payload(message.get('id') if isinstance(message, dict) else None, -32600, 'invalid JSON-RPC 2.0 message')
    request_id = message.get('id')
    method = message.get('method')
    if not isinstance(method, str):
        return error_payload(request_id, -32600, 'method 必须是字符串')
    is_notification = 'id' not in message
    if is_notification:
        return None
    params = message.get('params') or {}

    if method == 'initialize':
        requested = params.get('protocolVersion') if isinstance(params, dict) else None
        version = requested if isinstance(requested, str) and requested in PROTOCOL_VERSIONS else FALLBACK_PROTOCOL_VERSION
        return result_payload(request_id, {
            'protocolVersion': version,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
            'instructions': '密码 + TOTP 的 SSH 工具。凭据只从本地 secrets.json/环境变量读取；不提供写凭据工具；本机路径必须为绝对路径。',
        })
    if method == 'ping':
        return result_payload(request_id, {})
    if method == 'tools/list':
        return result_payload(request_id, {'tools': tool_definitions()})
    if method == 'tools/call':
        try:
            if not isinstance(params, dict):
                raise InvalidParams('params 必须是对象')
            name = params.get('name')
            if not isinstance(name, str):
                raise InvalidParams('params.name 必须是字符串')
            operation, args = validate_call(name, params.get('arguments') or {})
        except InvalidParams as error:
            return error_payload(request_id, -32602, str(error))
        try:
            value = getattr(runtime, operation)(args)
            return result_payload(request_id, tool_content(value))
        except McpToolError as error:
            return result_payload(request_id, tool_content(str(error), is_error=True))
        except Exception as error:  # keep protocol alive; diagnostics stay on stderr
            traceback.print_exc()
            return result_payload(request_id, tool_content(f'工具执行失败：{error}', is_error=True))
    return error_payload(request_id, -32601, f'method not found: {method}')


def write_message(message):
    data = json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def main():
    runtime = SshOtpRuntime()
    for raw in sys.stdin.buffer:
        if len(raw) > MAX_LINE_BYTES:
            write_message(error_payload(None, -32700, '请求行过大'))
            continue
        try:
            message = json.loads(raw.decode('utf-8'))
        except Exception as error:
            write_message(error_payload(None, -32700, f'JSON 解析失败：{error}'))
            continue
        response = handle(message, runtime)
        if response is not None:
            write_message(response)


if __name__ == '__main__':
    main()
