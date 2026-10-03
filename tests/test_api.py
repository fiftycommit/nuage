"""API checks use a temporary database; no host downloads or production writes."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
DATA = tempfile.TemporaryDirectory(prefix='nuage-tests-')
with patch.dict(os.environ, {
    'PORTAL_PASSWORD': 'test-only-password',
    'PORTAL_SESSION_SECRET': 'test-only-session-key-32-characters-long',
    'PORTAL_ROOT': DATA.name,
}), patch('threading.Thread.start'):
    spec = importlib.util.spec_from_file_location('nuage_under_test', ROOT/'app.py')
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app, base_url='https://portal.test')
        with app.connect() as connection:
            connection.execute('DELETE FROM jobs')
        (app.ROOT/'.deploying').unlink(missing_ok=True)
        app.login_failures.clear()

    def login(self):
        return self.client.post('/api/login', json={'password': 'test-only-password'})

    def test_private_data_requires_authentication(self):
        for path in ['/api/storage', '/api/jobs', '/api/auth/file']:
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get('/api/me').json(), {'authenticated': False})

    def test_login_cookie_is_secure_and_logout_clears_it(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        cookie = response.headers['set-cookie'].lower()
        for flag in ['httponly', 'secure', 'samesite=strict']:
            self.assertIn(flag, cookie)
        self.assertTrue(self.client.get('/api/me').json()['authenticated'])
        self.assertEqual(self.client.post('/api/logout').status_code, 200)
        self.assertFalse(self.client.get('/api/me').json()['authenticated'])

    def test_bad_password_and_forged_cookie_are_refused(self):
        with patch.object(app.time, 'sleep'):
            self.assertEqual(self.client.post('/api/login', json={'password': 'wrong'}).status_code, 401)
        self.client.cookies.set('portal_session', f'{int(time.time())}.fake.' + '0'*64)
        self.assertEqual(self.client.get('/api/jobs').status_code, 401)

    def test_mutations_refuse_foreign_origin_before_resolving_links(self):
        self.login()
        with patch.object(app, 'probe_all') as probe:
            response = self.client.post('/api/jobs', headers={'origin': 'https://evil.test'},
                                        json={'urls': ['https://mediafire.com/file/demo']})
            self.assertEqual(response.status_code, 403)
            probe.assert_not_called()

    def test_deployment_marker_blocks_new_jobs_without_probing(self):
        self.login()
        (app.ROOT/'.deploying').touch()
        with patch.object(app, 'probe_all') as probe:
            response = self.client.post('/api/jobs', json={'urls': ['https://mediafire.com/file/demo']})
            self.assertEqual(response.status_code, 503)
            probe.assert_not_called()

    def test_health_and_frontend_assets(self):
        self.assertEqual(self.client.get('/api/health').json()['status'], 'ok')
        self.assertEqual(self.client.get('/downloads').status_code, 200)
        response = self.client.get('/assets/file-tiles.js')
        self.assertEqual(response.status_code, 200)
        self.assertIn('javascript', response.headers['content-type'])

    def test_metadata_does_not_expose_original_urls_or_archive_password(self):
        now = int(time.time())
        job_id = 'a'*24
        with app.connect() as connection:
            connection.execute('INSERT INTO jobs(id,created,updated,state,mode,items,archive_password) VALUES(?,?,?,?,?,?,?)',
                (job_id, now, now, 'ready', 'rar', json.dumps([{'name': 'demo.rar',
                 'size': 10, 'host': 'MediaFire', 'url': 'https://mediafire.com/file/private-fixture'}]), 'archive-fixture-secret'))
        self.login()
        response = self.client.get('/api/jobs')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('archive-fixture-secret', response.text)
        self.assertNotIn('private-fixture', response.text)

    def test_provider_allowlist_and_archive_traversal(self):
        for url in ['http://mediafire.com/file/x', 'https://mediafire.com.evil.test/x',
                    'https://user:pass@mediafire.com/x', 'https://127.0.0.1/x']:
            with self.assertRaises(ValueError):
                app.provider(url)
        self.assertEqual(app.provider('https://www.mediafire.com/file/x'), 'MediaFire')
        from types import SimpleNamespace
        with patch.object(app.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='../escape.bin\n')):
            with self.assertRaises(RuntimeError):
                app.safe_archive_entries(Path('demo.rar'), '')


if __name__ == '__main__':
    unittest.main()
