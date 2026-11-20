#!/usr/bin/env python
"""dsh-ssh-otp 的常驻 SSH 服务。

由 DSH 插件的 Node 侧以子进程方式启动，通过 stdin/stdout 逐行 JSON 通信。
stdout 只用于协议消息，所有日志一律写 stderr。

协议
----
请求：{"id": 1, "op": "exec", "alias": "...", "conn": {...}, ...}
响应：{"id": 1, "ok": true, ...} / {"id": 1, "ok": false, "error": "..."}

op：
  ping      —— 存活探测
  status    —— 列出当前活跃连接
  exec      —— 执行远程命令
  upload    —— 本地上传至远程（SFTP）
  download  —— 远程下载到本地（SFTP）
  close     —— 关闭指定（或不指定=全部）连接

conn 结构：
  {"host": "cluster.example.org", "port": 22, "user": "your-username",
   "auth": {"kind": "password_totp" | "password",
            "password": "...", "totp_seed": "..."}}
"""
import base64  # noqa: F401  (保留：部分环境用 base64 载荷排障)
import hashlib
import hmac
import json
import os
import re
import struct
import sys
import tempfile
import threading
import time

import paramiko

SPILL_DIR = os.path.join(tempfile.gettempdir(), 'dsh-ssh-otp')
IDLE_SECONDS = 600          # 空闲 10 分钟断开
INLINE_BYTES = 512 * 1024   # 单流内联上限，超出则落临时文件
AUTH_WAIT_MAX = 35          # 撞验证码防重放时最多等这么久（秒）


def log(message):
    sys.stderr.write('[ssh-otp] %s\n' % message)
    sys.stderr.flush()


# ---------------------------------------------------------------- TOTP

def base32_decode(secret):
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
    clean = re.sub(r'[^A-Z2-7]', '', (secret or '').upper())
    bits = value = 0
    out = bytearray()
    for ch in clean:
        value = (value << 5) | alphabet.index(ch)
        bits += 5
        if bits >= 8:
            out.append((value >> (bits - 8)) & 0xff)
            bits -= 8
    return bytes(out)


def totp(secret, now=None):
    key = base32_decode(secret)
    if not key:
        raise ValueError('totp_seed 不是合法的 base32')
    counter = int(now if now is not None else time.time()) // 30
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0f
    code = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff
    return str(code % 1000000).zfill(6)


def wait_next_window(seconds_max=AUTH_WAIT_MAX):
    remainder = 30 - (time.time() % 30)
    if remainder > seconds_max:
        return False
    time.sleep(remainder + 1.2)
    return True


# ---------------------------------------------------------------- 连接

class Connection:
    def __init__(self, alias, spec):
        self.alias = alias
        self.spec = spec
        self.transport = None
        self.sftp = None
        self.last_used = 0.0

    # -- 建立连接 -------------------------------------------------
    def _open_transport(self):
        auth = self.spec.get('auth') or {}
        kind = auth.get('kind')
        password = auth.get('password') or ''
        seed = auth.get('totp_seed') or ''

        def handler(title, instructions, prompts):
            answers = []
            for prompt, _echo in prompts:
                if re.search(r'code|verif|otp|token|验证码', prompt, re.I):
                    answers.append(totp(seed))
                else:
                    answers.append(password)
            return answers

        transport = paramiko.Transport((self.spec['host'], int(self.spec.get('port') or 22)))
        transport.connect()
        try:
            if kind == 'password_totp':
                transport.auth_interactive(self.spec['user'], handler)
            else:
                transport.auth_password(self.spec['user'], password)
        except paramiko.AuthenticationException:
            # 2FA 防重放：换到下一个 30 秒窗口再试一次
            transport.close()
            if kind == 'password_totp' and wait_next_window():
                log('认证失败，已等待下一个验证码窗口后重试')
                transport = paramiko.Transport((self.spec['host'], int(self.spec.get('port') or 22)))
                transport.connect()
                transport.auth_interactive(self.spec['user'], handler)
            else:
                raise
        if not transport.is_authenticated():
            transport.close()
            raise paramiko.AuthenticationException('认证未通过')
        log('已连接 %s (%s@%s)' % (self.alias, self.spec['user'], self.spec['host']))
        return transport

    def ensure(self):
        if self.transport is not None and self.transport.is_active():
            self.last_used = time.time()
            return self.transport
        if self.transport is not None:
            log('%s 的连接已失效，重连' % self.alias)
            self._discard()
        self.transport = self._open_transport()
        self.last_used = time.time()
        return self.transport

    def _discard(self):
        try:
            if self.sftp is not None:
                self.sftp.close()
        except Exception:
            pass
        self.sftp = None
        try:
            if self.transport is not None:
                self.transport.close()
        except Exception:
            pass
        self.transport = None

    def close(self):
        self._discard()

    def client(self):
        if self.sftp is None:
            self.sftp = paramiko.SFTPClient.from_transport(self.ensure())
        return self.sftp

    # -- 执行 ------------------------------------------------------
    def exec(self, command, timeout_ms):
        transport = self.ensure()
        chan = transport.open_session()
        chan.exec_command(command)
        deadline = time.time() + max(1.0, (timeout_ms or 120000) / 1000.0)

        out = bytearray()
        err = bytearray()
        totals = {'stdout': 0, 'stderr': 0}
        spills = {'stdout': None, 'stderr': None}  # stream -> (path, handle)
        timed_out = False

        def feed(stream, chunk):
            """内联部分只留头部；一旦超出上限，就把完整内容写进临时文件。"""
            totals[stream] += len(chunk)
            entry = spills[stream]
            if entry is not None:
                entry[1].write(chunk)
                return
            target = out if stream == 'stdout' else err
            room = INLINE_BYTES - len(target)
            if len(chunk) <= room:
                target.extend(chunk)
                return
            os.makedirs(SPILL_DIR, exist_ok=True)
            path = os.path.join(SPILL_DIR, '%s-%d-%d.txt' % (stream, os.getpid(), int(time.time() * 1000)))
            handle = open(path, 'wb')
            handle.write(bytes(target))
            handle.write(chunk)
            spills[stream] = (path, handle)
            target.extend(chunk[:room])

        idle_after_exit = None
        while True:
            if time.time() > deadline:
                timed_out = True
                try:
                    chan.close()
                except Exception:
                    pass
                break
            moved = False
            # 先把内核/paramiko 缓冲区里的数据全部读干净，避免单次读取后误判“没数据了”
            while chan.recv_ready():
                feed('stdout', chan.recv(65536))
                moved = True
            while chan.recv_stderr_ready():
                feed('stderr', chan.recv_stderr(65536))
                moved = True
            if moved:
                idle_after_exit = None
                continue
            if chan.exit_status_ready():
                # 退出码就绪不代表输出读完：等服务端 EOF；个别服务端不发 EOF，则静默 1 秒后收工
                if chan.eof_received:
                    break
                if idle_after_exit is None:
                    idle_after_exit = time.time()
                elif time.time() - idle_after_exit > 1.0:
                    break
            time.sleep(0.02)

        exit_code = -1 if timed_out else chan.recv_exit_status()
        self.last_used = time.time()
        result = {
            'exitCode': exit_code,
            'stdout': out.decode('utf-8', 'replace'),
            'stderr': err.decode('utf-8', 'replace'),
            'stdoutBytes': totals['stdout'],
            'stderrBytes': totals['stderr'],
            'timedOut': timed_out,
        }
        # 超出内联上限时，完整内容在临时文件里，Node 侧直接引用该路径
        for stream in ('stdout', 'stderr'):
            entry = spills[stream]
            if entry is not None:
                entry[1].close()
                result[stream + 'SpillPath'] = entry[0]
        return result

    # -- 传输 ------------------------------------------------------
    def upload(self, local_path, remote_path, overwrite):
        sftp = self.client()
        if not overwrite:
            # 注意：FileExistsError 是 IOError 的子类，raise 必须放在 try 之外，
            # 否则会被 except IOError 自己吞掉（这个坑踩过一次）。
            try:
                info = sftp.stat(remote_path)
            except IOError:
                info = None
            if info is not None:
                raise FileExistsError('远程已存在 %s（%d 字节），如需覆盖请显式传 overwrite=true'
                                      % (remote_path, info.st_size))
        sftp.put(local_path, remote_path)
        info = sftp.stat(remote_path)
        self.last_used = time.time()
        return {'bytes': info.st_size, 'remotePath': remote_path}

    def download(self, remote_path, local_path, overwrite):
        sftp = self.client()
        if os.path.exists(local_path) and not overwrite:
            raise FileExistsError('本地已存在 %s，如需覆盖请显式传 overwrite=true' % local_path)
        parent = os.path.dirname(os.path.abspath(local_path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        # 先下到临时文件再改名：失败时不留半截文件，也不会把下次重试卡在覆盖保护上
        tmp_path = '%s.part-%d' % (local_path, os.getpid())
        done = False
        try:
            sftp.get(remote_path, tmp_path)
            done = True
        except FileNotFoundError:
            raise FileNotFoundError('远程文件不存在：%s' % remote_path)
        finally:
            if not done:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        os.replace(tmp_path, local_path)
        self.last_used = time.time()
        return {'bytes': os.path.getsize(local_path), 'localPath': local_path}


CONNECTIONS = {}


def get_connection(alias, spec):
    existing = CONNECTIONS.get(alias)
    if existing is not None and existing.spec == spec:
        existing.ensure()
        return existing
    if existing is not None:
        existing.close()
    conn = Connection(alias, spec)
    CONNECTIONS[alias] = conn
    return conn


def reaper():
    while True:
        time.sleep(60)
        now = time.time()
        for alias, conn in list(CONNECTIONS.items()):
            if conn.last_used and now - conn.last_used > IDLE_SECONDS:
                log('%s 空闲超时，断开' % alias)
                conn.close()
                CONNECTIONS.pop(alias, None)


# ---------------------------------------------------------------- 协议

def handle(request):
    op = request.get('op')
    if op == 'ping':
        return {'pong': True, 'pid': os.getpid()}
    if op == 'status':
        now = time.time()
        return {'connections': [
            {'alias': a, 'alive': bool(c.transport and c.transport.is_active()),
             'idleSeconds': int(now - c.last_used) if c.last_used else None}
            for a, c in CONNECTIONS.items()]}
    if op == 'close':
        alias = request.get('alias')
        targets = [alias] if alias else list(CONNECTIONS.keys())
        for name in targets:
            conn = CONNECTIONS.pop(name, None)
            if conn is not None:
                conn.close()
        return {'closed': targets}

    alias = request.get('alias')
    spec = request.get('conn')
    if not alias or not spec:
        raise ValueError('缺少 alias 或 conn')

    conn = get_connection(alias, spec)

    if op == 'exec':
        return conn.exec(request.get('command') or '', request.get('timeoutMs'))
    if op == 'upload':
        return conn.upload(request['localPath'], request['remotePath'], bool(request.get('overwrite')))
    if op == 'download':
        return conn.download(request['remotePath'], request['localPath'], bool(request.get('overwrite')))
    raise ValueError('未知 op: %s' % op)


def main():
    """主循环：每行请求交给独立线程，按 alias 加锁保证同主机串行、跨主机并行。"""
    threading.Thread(target=reaper, daemon=True).start()
    log('服务已启动，pid=%d' % os.getpid())
    out_lock = threading.Lock()
    alias_locks = {}
    locks_guard = threading.Lock()

    def lock_for(alias):
        with locks_guard:
            if alias not in alias_locks:
                alias_locks[alias] = threading.Lock()
            return alias_locks[alias]

    def run(request):
        req_id = request.get('id')
        op = request.get('op')
        try:
            if op in ('ping', 'status'):
                payload = handle(request)
            else:
                alias = request.get('alias') or ''
                with lock_for(alias or '__all__'):
                    payload = handle(request)
            payload.update({'id': req_id, 'ok': True})
        except FileExistsError as error:
            payload = {'id': req_id, 'ok': False, 'error': str(error), 'code': 'EEXIST'}
        except FileNotFoundError as error:
            payload = {'id': req_id, 'ok': False, 'error': str(error), 'code': 'ENOENT'}
        except Exception as error:  # noqa: BLE001
            payload = {'id': req_id, 'ok': False, 'error': '%s: %s' % (type(error).__name__, error)}
        try:
            line = json.dumps(payload, ensure_ascii=False)
            with out_lock:
                sys.stdout.write(line + '\n')
                sys.stdout.flush()
        except Exception as error:  # noqa: BLE001
            log('写回响应失败: %s' % error)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception as error:
            with out_lock:
                sys.stdout.write(json.dumps({'id': None, 'ok': False, 'error': 'BAD_JSON: %s' % error}) + '\n')
                sys.stdout.flush()
            continue
        threading.Thread(target=run, args=(request,), daemon=True).start()


if __name__ == '__main__':
    main()
