"""Administrator login and environment configuration regressions."""
import os
import tempfile
import unittest
from unittest.mock import patch

from evoagent.auth import AuthManager
from evoagent.config import load_dotenv
from evoagent.store import TaskStore


class DotenvTests(unittest.TestCase):

    def test_loads_valid_assignments_and_quoted_values(self):
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False) as handle:
            handle.write('# comment\n')
            handle.write('export EVOAGENT_LLM_PROVIDER=deepseek\n')
            handle.write('EVOAGENT_DEEPSEEK_API_KEY="test-key"\n')
            handle.write('invalid line\n')
            path = handle.name
        try:
            with patch.dict(os.environ, {}, clear=True):
                load_dotenv([path])
                self.assertEqual('deepseek', os.environ['EVOAGENT_LLM_PROVIDER'])
                self.assertEqual('test-key', os.environ['EVOAGENT_DEEPSEEK_API_KEY'])
        finally:
            os.unlink(path)

    def test_process_environment_has_priority(self):
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False) as handle:
            handle.write('EVOAGENT_LLM_PROVIDER=deepseek\n')
            path = handle.name
        try:
            with patch.dict(os.environ, {'EVOAGENT_LLM_PROVIDER': 'custom'}, clear=True):
                load_dotenv([path])
                self.assertEqual('custom', os.environ['EVOAGENT_LLM_PROVIDER'])
        finally:
            os.unlink(path)


class AdministratorTests(unittest.TestCase):

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        self.store = TaskStore(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_login_rbac_and_tenant_task_isolation(self):
        auth = AuthManager(
            self.store, 'a' * 32, bootstrap_username='admin',
            bootstrap_password='correct-horse', default_tenant_id='tenant-a',
        )
        token = auth.login('admin', 'correct-horse')['access_token']
        principal = auth.authenticate('Bearer ' + token)
        self.assertTrue(principal.can('manage'))
        self.store.create('a', 'org/a', 1, {}, 'tenant-a')
        self.store.create('b', 'org/b', 2, {}, 'tenant-b')
        self.assertIsNotNone(self.store.get('a', principal.tenant_id))
        self.assertIsNone(self.store.get('b', principal.tenant_id))
        self.assertEqual(['a'], [item['id'] for item in self.store.list_tasks(10, 'tenant-a')])


