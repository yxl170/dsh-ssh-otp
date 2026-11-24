"""End-to-end MCP stdio protocol tests. No real SSH login is performed."""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / 'mcp' / 'ssh_otp_mcp.py'


class McpClient:
    def __init__(self, env):
        self.proc = subprocess.Popen(
            [os.environ.get('SSH_OTP_TEST_PYTHON', sys.executable), str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.responses = queue.Queue()
        self.stderr = []
        self.next_id = 1
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        for raw in self.proc.stdout:
            self.responses.put(json.loads(raw.decode('utf-8')))

    def _read_stderr(self):
        for raw in self.proc.stderr:
            self.stderr.append(raw.decode('utf-8', 'replace'))

    def send(self, method, params=None, notification=False):
        message = {'jsonrpc': '2.0', 'method': method}
        if not notification:
            message['id'] = self.next_id
            self.next_id += 1
        if params is not None:
            message['params'] = params
        self.proc.stdin.write((json.dumps(message) + '\n').encode('utf-8'))
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self.send(method, params)
        response = self.responses.get(timeout=20)
        assert response['id'] == self.next_id - 1, response
        return response

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.config = home / 'config.json'
        self.secrets = home / 'secrets.json'
        self.config.write_text(json.dumps({
            'timeoutMs': 120000,
            'retryOnAuthFailure': False,
            'hosts': {
                'demo': {
                    'host': '127.0.0.1', 'port': 22, 'user': 'demo',
                    'auth': {'kind': 'password_totp', 'passwordRef': 'DEMO_SSH_PASSWORD', 'totpRef': 'DEMO_TOTP_SEED'},
                },
            },
        }), encoding='utf-8')
        self.secret_values = {'DEMO_SSH_PASSWORD': 'fake-password-not-real', 'DEMO_TOTP_SEED': 'JBSWY3DPEHPK3PXP'}
        self.secrets.write_text(json.dumps(self.secret_values), encoding='utf-8')
        env = os.environ.copy()
        env['SSH_OTP_CONFIG'] = str(self.config)
        env['SSH_OTP_SECRETS'] = str(self.secrets)
        env['PYTHONIOENCODING'] = 'utf-8'
        self.client = McpClient(env)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_full_protocol_flow(self):
        initialized = self.client.request('initialize', {
            'protocolVersion': '2025-03-26',
            'capabilities': {},
            'clientInfo': {'name': 'protocol-test', 'version': '1'},
        })
        result = initialized['result']
        self.assertEqual(result['protocolVersion'], '2025-03-26')
        self.assertEqual(result['serverInfo']['name'], 'dsh-ssh-otp-mcp')
        self.assertFalse(result['capabilities']['tools']['listChanged'])

        # A notification must not produce a response; the next response is the ping's.
        self.client.send('notifications/initialized', notification=True)
        self.assertEqual(self.client.request('ping')['result'], {})

        tools = self.client.request('tools/list')['result']['tools']
        self.assertEqual({tool['name'] for tool in tools}, {
            'ssh_otp_hosts', 'ssh_otp_exec', 'ssh_otp_upload', 'ssh_otp_download', 'ssh_otp_doctor',
        })
        self.assertEqual(
            next(tool for tool in tools if tool['name'] == 'ssh_otp_exec')['inputSchema']['required'],
            ['host', 'command'],
        )

        hosts_result = self.client.request('tools/call', {
            'name': 'ssh_otp_hosts', 'arguments': {},
        })['result']
        self.assertFalse(hosts_result['isError'])
        hosts = json.loads(hosts_result['content'][0]['text'])['hosts']
        self.assertEqual(hosts[0]['alias'], 'demo')
        self.assertTrue(hosts[0]['passwordReady'])
        self.assertTrue(hosts[0]['totpReady'])

        doctor_result = self.client.request('tools/call', {
            'name': 'ssh_otp_doctor', 'arguments': {},
        })['result']
        doctor = json.loads(doctor_result['content'][0]['text'])
        self.assertIn('python', doctor)
        self.assertIn('paramiko', doctor)
        self.assertEqual(doctor['secretCount'], 2)
        self.assertIn('tcp', doctor['hosts'][0])

        unknown = self.client.request('no/such/method')
        self.assertEqual(unknown['error']['code'], -32601)
        unknown_tool = self.client.request('tools/call', {'name': 'nope', 'arguments': {}})
        self.assertEqual(unknown_tool['error']['code'], -32602)
        missing = self.client.request('tools/call', {'name': 'ssh_otp_exec', 'arguments': {'host': 'demo'}})
        self.assertEqual(missing['error']['code'], -32602)

    def test_no_secret_or_traceback_leaks(self):
        self.client.request('initialize', {'protocolVersion': ['not-a-version']})
        self.client.request('tools/call', {'name': 'ssh_otp_hosts', 'arguments': {}})
        self.client.request('tools/call', {'name': 'ssh_otp_exec', 'arguments': {'host': 'demo'}})
        self.client.request('no/such/method')
        stderr = ''.join(self.client.stderr)
        for value in self.secret_values.values():
            self.assertNotIn(value, stderr)
        self.assertNotIn('Traceback', stderr)


if __name__ == '__main__':
    unittest.main()
