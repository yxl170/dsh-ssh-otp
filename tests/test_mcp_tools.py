"""Unit tests for the standalone MCP runtime (fake connections only)."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('mcp_runtime_under_test', ROOT / 'mcp' / 'runtime.py')
runtime_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime_module)
McpToolError = runtime_module.McpToolError
SshOtpRuntime = runtime_module.SshOtpRuntime
real_service = runtime_module.load_ssh_service()


class FakeConnection:
    def __init__(self, behavior):
        self.behavior = behavior
        self.retry_on_auth_failure = False

    def exec(self, command, timeout):
        action = self.behavior(command)
        if isinstance(action, Exception):
            raise action
        return action

    def upload(self, local, remote, overwrite):
        return {'bytes': Path(local).stat().st_size, 'remotePath': remote}

    def download(self, remote, local, overwrite):
        raise FileNotFoundError(f'远程文件不存在：{remote}')


class FakeService:
    AuthFailed = real_service.AuthFailed

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = 0
        self.specs = []

    @staticmethod
    def reaper():
        return None

    def get_connection(self, alias, spec):
        self.calls += 1
        self.specs.append(spec)
        return FakeConnection(self.behavior)


class RuntimeTest(unittest.TestCase):
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
        self.secrets.write_text(json.dumps({
            'DEMO_SSH_PASSWORD': 'fake-password-not-real',
            'DEMO_TOTP_SEED': 'JBSWY3DPEHPK3PXP',
        }), encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def runtime(self, behavior):
        return SshOtpRuntime(str(self.config), str(self.secrets), service=FakeService(behavior))

    def test_hosts_and_environment_override(self):
        rt = self.runtime(lambda command: {})
        row = rt.hosts()['hosts'][0]
        self.assertEqual(row['alias'], 'demo')
        self.assertTrue(row['passwordReady'])
        self.assertTrue(row['totpReady'])
        with mock.patch.dict(os.environ, {'SSH_OTP_SECRET_DEMO_SSH_PASSWORD': 'override-not-real'}):
            self.assertTrue(rt.hosts()['hosts'][0]['passwordReady'])

    def test_missing_host_and_secret_are_actionable(self):
        self.secrets.write_text('{}', encoding='utf-8')
        rt = self.runtime(lambda command: {})
        with self.assertRaisesRegex(McpToolError, 'DEMO_SSH_PASSWORD'):
            rt.execute({'host': 'demo', 'command': 'hostname'})
        with self.assertRaisesRegex(McpToolError, '未配置主机'):
            rt.execute({'host': 'missing', 'command': 'hostname'})

    def test_exit_code_and_timeout_passthrough(self):
        rt = self.runtime(lambda command: {
            'exitCode': 7, 'stdout': 'out\n', 'stderr': 'err\n', 'timedOut': False,
            'stdoutBytes': 4, 'stderrBytes': 4,
        })
        value = rt.execute({'host': 'demo', 'command': 'exit 7'})
        self.assertEqual(value['exitCode'], 7)
        self.assertEqual(value['stdout'], 'out\n')
        self.assertEqual(value['stderr'], 'err\n')
        self.assertFalse(value['timedOut'])

    def test_service_spill_is_complete_and_reported(self):
        spill = Path(self.tmp.name) / 'full.log'
        spill.write_text('x' * 600000, encoding='utf-8')
        rt = self.runtime(lambda command: {
            'exitCode': 0, 'stdout': 'x' * 512000, 'stderr': '',
            'stdoutBytes': 600000, 'stdoutSpillPath': str(spill), 'timedOut': False,
        })
        value = rt.execute({'host': 'demo', 'command': 'big'})
        self.assertEqual(value['bytesTotal'], 600000)
        self.assertTrue(value['truncated'])
        self.assertEqual(value['spillPath'], str(spill))
        self.assertEqual(Path(value['spillPath']).stat().st_size, 600000)
        self.assertLess(len(value['stdout']), 40000)

    def test_runtime_spill_writes_full_stream(self):
        with mock.patch.object(runtime_module.Path, 'home', return_value=Path(self.tmp.name)):
            rt = self.runtime(lambda command: {
                'exitCode': 0, 'stdout': 'x' * 100000, 'stderr': '',
                'stdoutBytes': 100000, 'timedOut': False,
            })
            value = rt.execute({'host': 'demo', 'command': 'big'})
        self.assertTrue(value['truncated'])
        self.assertEqual(Path(value['spillPath']).stat().st_size, 100000)
        self.assertEqual(value['bytesTotal'], 100000)

    def test_two_auth_failures_trip_local_gate(self):
        failure = real_service.AuthFailed([{'prompt': 'Password: ', 'kind': 'password'}], 1)
        rt = self.runtime(lambda command: failure)
        for expected in (1, 2):
            with self.assertRaisesRegex(McpToolError, '认证失败'):
                rt.execute({'host': 'demo', 'command': 'hostname'})
            self.assertEqual(rt.service.calls, expected)
        with self.assertRaisesRegex(McpToolError, 'fail2ban'):
            rt.execute({'host': 'demo', 'command': 'hostname'})
        self.assertEqual(rt.service.calls, 2, 'third call must be blocked locally')

    def test_success_resets_auth_failure_count(self):
        state = {'fail': True}

        def behavior(command):
            if state['fail']:
                return real_service.AuthFailed([{'prompt': 'Password: ', 'kind': 'password'}], 1)
            return {'exitCode': 0, 'stdout': 'ok', 'stderr': '', 'stdoutBytes': 2, 'timedOut': False}

        rt = self.runtime(behavior)
        with self.assertRaises(McpToolError):
            rt.execute({'host': 'demo', 'command': 'hostname'})
        state['fail'] = False
        self.assertEqual(rt.execute({'host': 'demo', 'command': 'hostname'})['stdout'], 'ok')
        state['fail'] = True
        with self.assertRaisesRegex(McpToolError, '认证失败'):
            rt.execute({'host': 'demo', 'command': 'hostname'})
        self.assertEqual(rt.service.calls, 3)

    def test_transfer_validation_and_clean_errors(self):
        rt = self.runtime(lambda command: {})
        with self.assertRaisesRegex(McpToolError, '绝对路径'):
            rt.upload({'host': 'demo', 'localPath': 'relative.txt', 'remotePath': '/tmp/x'})
        with self.assertRaisesRegex(McpToolError, '远程文件不存在'):
            rt.download({'host': 'demo', 'remotePath': '/tmp/nope', 'localPath': str(Path(self.tmp.name) / 'x')})

    def test_invalid_config_rejected(self):
        parsed = json.loads(self.config.read_text(encoding='utf-8'))
        parsed['hosts']['demo']['port'] = 70000
        self.config.write_text(json.dumps(parsed), encoding='utf-8')
        rt = self.runtime(lambda command: {})
        with self.assertRaisesRegex(McpToolError, '端口'):
            rt.hosts()


class AuthPolicyTest(unittest.TestCase):
    def setUp(self):
        self.original_transport = real_service.paramiko.Transport
        self.original_wait = real_service.wait_next_window
        self.instances = []

        test = self

        class FakeTransport:
            prompts = [('Password: ', False)]
            succeed = False

            def __init__(self, address):
                self.authenticated = False
                test.instances.append(self)

            def connect(self):
                return None

            def close(self):
                return None

            def is_authenticated(self):
                return self.authenticated

            def auth_interactive(self, user, handler):
                answers = handler('', '', self.prompts)
                self.last_answers = answers
                if self.succeed:
                    self.authenticated = True
                    return
                raise real_service.paramiko.AuthenticationException('fake failure')

        self.fake_transport = FakeTransport
        real_service.paramiko.Transport = FakeTransport
        real_service.wait_next_window = lambda *args, **kwargs: True

    def tearDown(self):
        real_service.paramiko.Transport = self.original_transport
        real_service.wait_next_window = self.original_wait

    def connection(self, retry=False):
        conn = real_service.Connection('demo', {
            'host': '127.0.0.1', 'port': 22, 'user': 'demo',
            'auth': {'kind': 'password_totp', 'password': 'fake-password-not-real', 'totp_seed': 'JBSWY3DPEHPK3PXP'},
        })
        conn.retry_on_auth_failure = retry
        return conn

    def test_default_is_one_attempt_even_with_totp_prompt(self):
        self.fake_transport.prompts = [('Verification code: ', False), ('Password: ', False)]
        with self.assertRaises(real_service.AuthFailed) as caught:
            self.connection(retry=False)._open_transport()
        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(len(self.instances), 1)

    def test_password_only_failure_never_retries(self):
        with self.assertRaises(real_service.AuthFailed) as caught:
            self.connection(retry=True)._open_transport()
        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(len(self.instances), 1)

    def test_explicit_retry_only_with_totp_prompt(self):
        self.fake_transport.prompts = [('Verification code: ', False), ('Password: ', False)]
        with self.assertRaises(real_service.AuthFailed) as caught:
            self.connection(retry=True)._open_transport()
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(len(self.instances), 2)

    def test_totp_before_password_answers_by_prompt_kind(self):
        self.fake_transport.prompts = [('Verification code: ', False), ('Password: ', False)]
        self.fake_transport.succeed = True
        self.connection()._open_transport()
        answers = self.instances[0].last_answers
        self.assertEqual(len(answers), 2)
        self.assertRegex(answers[0], r'^\d{6}$')
        self.assertEqual(answers[1], 'fake-password-not-real')


if __name__ == '__main__':
    unittest.main()
