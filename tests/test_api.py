"""API checks use a temporary database; no host downloads or production writes."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
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

    def deletion_fixture(self, state='ready', package_state='ready'):
        job_id = 'c'*24
        now = int(time.time())
        with app.connect() as connection:
            connection.execute('INSERT INTO jobs(id,created,updated,state,mode,items,package_state) VALUES(?,?,?,?,?,?,?)',
                               (job_id, now, now, state, 'rar', '[]', package_state))
        for root in (app.PRIVATE, app.PUBLIC):
            directory = root/job_id
            directory.mkdir(parents=True, exist_ok=True)
            (directory/'fixture.bin').write_bytes(b'fixture')
        (app.PUBLIC/job_id/f'download-{job_id}.zip').write_bytes(b'zip-fixture')
        return job_id

    def test_delete_removes_job_archives_files_and_zip_only_for_this_job(self):
        job_id = self.deletion_fixture()
        other = app.PUBLIC/('d'*24)
        other.mkdir(exist_ok=True)
        (other/'keep.bin').write_bytes(b'keep')
        self.login()
        self.assertEqual(self.client.delete(f'/api/jobs/{job_id}').json(), {'id': job_id, 'deleted': True})
        self.assertFalse((app.PRIVATE/job_id).exists())
        self.assertFalse((app.PUBLIC/job_id).exists())
        self.assertTrue((other/'keep.bin').exists())
        self.assertEqual(self.client.get(f'/api/jobs/{job_id}').status_code, 404)
        self.assertEqual(self.client.delete(f'/api/jobs/{job_id}').status_code, 404)

    def test_delete_requires_login_same_origin_and_no_deployment(self):
        job_id = self.deletion_fixture()
        url = f'/api/jobs/{job_id}'
        self.assertEqual(self.client.delete(url).status_code, 401)
        self.login()
        self.assertEqual(self.client.delete(url, headers={'origin':'https://evil.test'}).status_code, 403)
        self.assertEqual(self.client.delete('/api/jobs/not-a-valid-id').status_code, 400)
        (app.ROOT/'.deploying').touch()
        self.assertEqual(self.client.delete(url).status_code, 503)
        self.assertTrue((app.PUBLIC/job_id).exists())

    def test_delete_queued_job_prevents_worker_from_starting_it(self):
        job_id = self.deletion_fixture(state='queued', package_state='none')
        self.login()
        self.assertEqual(self.client.delete(f'/api/jobs/{job_id}').status_code, 200)
        self.assertIsNone(app.claim_next_job())

    def test_delete_refuses_running_jobs_and_claimed_zip(self):
        job_id = self.deletion_fixture(state='queued', package_state='none')
        self.login()
        self.assertEqual(app.claim_next_job()['state'], 'downloading')
        for state in ('downloading', 'extracting', 'publishing'):
            app.update(job_id, state=state)
            self.assertEqual(self.client.delete(f'/api/jobs/{job_id}').status_code, 409)
        app.update(job_id, state='ready', package_state='queued')
        self.assertEqual(app.claim_next_job(package=True)['package_state'], 'building')
        self.assertEqual(self.client.delete(f'/api/jobs/{job_id}').status_code, 409)
        self.assertTrue((app.PUBLIC/job_id).exists())

    def test_delete_failed_cleanup_keeps_job_for_retry(self):
        job_id = self.deletion_fixture(state='failed', package_state='failed')
        self.login()
        with patch.object(app.shutil, 'rmtree', side_effect=OSError('fixture disk error')):
            self.assertEqual(self.client.delete(f'/api/jobs/{job_id}').status_code, 500)
        self.assertEqual(app.fetch(job_id)['state'], 'failed')

    def test_delete_unlinks_symlink_without_removing_its_target(self):
        job_id = self.deletion_fixture(state='expired', package_state='expired')
        app.shutil.rmtree(app.PRIVATE/job_id)
        external = app.ROOT/'external-fixture'
        external.mkdir(exist_ok=True)
        (external/'keep.bin').write_bytes(b'keep')
        (app.PRIVATE/job_id).symlink_to(external, target_is_directory=True)
        self.login()
        self.assertEqual(self.client.delete(f'/api/jobs/{job_id}').status_code, 200)
        self.assertTrue((external/'keep.bin').exists())
        self.assertFalse((app.PRIVATE/job_id).is_symlink())

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

    def test_rootz_probe_queue_and_worker_dispatch(self):
        self.login()
        url = 'https://www.rootz.so/d/private-fixture'
        item = {'name': 'fixture.rar', 'size': 1024, 'host': 'Rootz', 'url': url}
        with patch.object(app.rootz, 'metadata', return_value=item), patch.object(
                app.shutil, 'disk_usage', return_value=SimpleNamespace(free=100 * 1024**3)):
            response = self.client.post('/api/probe', json={'urls': [url]})
            self.assertTrue(response.json()['all_ready'])
            self.assertNotIn('url', response.json()['items'][0])
            queued = self.client.post('/api/jobs', json={'urls': [url]})
        self.assertEqual(queued.status_code, 200)
        with patch.object(app.rootz, 'download') as download, patch.object(app, 'run_aria2') as aria:
            app.download_one(item, app.PRIVATE, queued.json()['id'])
            download.assert_called_once()
            aria.assert_not_called()

    def test_filekeeper_probe_hides_direct_link_and_worker_uses_fresh_link(self):
        self.login()
        url = 'https://filekeeper.net/abcdef123456'
        body = b'7z\xbc\xaf\x27\x1c' + b'fixture-content'
        item = {'name': 'fixture.7z', 'size': len(body), 'host': 'Filekeeper', 'url': url}
        with patch.object(app.filekeeper, 'metadata', return_value=item), patch.object(
                app.shutil, 'disk_usage', return_value=SimpleNamespace(free=100 * 1024**3)):
            response = self.client.post('/api/probe', json={'urls': [url]})
            self.assertTrue(response.json()['all_ready'])
            self.assertNotIn('direct', response.json()['items'][0])
            self.assertNotIn('url', response.json()['items'][0])
            queued = self.client.post('/api/jobs', json={'urls': [url]})
        self.assertEqual(queued.status_code, 200)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            direct = 'https://tunnel1.dlproxy.uk/fresh-fixture'
            def transfer(command, *args):
                self.assertEqual(command[-1], direct)
                self.assertIn('--continue=true', command)
                self.assertEqual(command[command.index('--out') + 1], item['name'])
                (folder/item['name']).write_bytes(body)
                return 0
            with patch.object(app.filekeeper, 'resolve', return_value={**item, 'direct': direct}), patch.object(
                    app, 'run_aria2', side_effect=transfer):
                app.download_one(item, folder, queued.json()['id'])
        progress = json.loads(app.fetch(queued.json()['id'])['progress'])
        self.assertEqual(progress[item['name']]['completed'], len(body))

    def test_filekeeper_changed_or_invalid_archive_never_completes(self):
        item = {'name': 'fixture.7z', 'size': 12, 'host': 'Filekeeper',
                'url': 'https://filekeeper.net/abcdef123456'}
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with patch.object(app.filekeeper, 'resolve', return_value={**item, 'size': 13}), patch.object(
                    app, 'run_aria2') as aria, self.assertRaises(RuntimeError):
                app.download_one(item, folder, 'f'*24)
            aria.assert_not_called()
            def bad_transfer(*args):
                (folder/item['name']).write_bytes(b'x' * 12)
                return 0
            with patch.object(app.filekeeper, 'resolve', return_value={
                    **item, 'direct': 'https://tunnel1.dlproxy.uk/fixture'}), patch.object(
                    app, 'run_aria2', side_effect=bad_transfer), patch.object(
                    app, 'update_item_progress') as progress, self.assertRaises(RuntimeError):
                app.download_one(item, folder, 'f'*24)
            progress.assert_not_called()

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

    def test_unrar_reports_live_progress_and_confirms_success(self):
        real_popen = subprocess.Popen
        script = (
            'import os,sys,time; sys.stdin.buffer.read(); '
            'os.write(1,b"Extracting archive-98%.rar\\n\\b\\b\\b\\b 2"); time.sleep(.05); '
            'os.write(1,b"5%"); time.sleep(.05); '
            'os.write(1,b"\\b\\b\\b\\b 75%"); time.sleep(.05); '
            'os.write(1,b"\\b\\b\\b\\b100%")'
        )
        def start(command, **kwargs):
            self.assertNotIn('archive-fixture-secret', command)
            return real_popen([sys.executable, '-c', script], **kwargs)
        with patch.object(app.subprocess, 'Popen', side_effect=start), patch.object(app, 'update') as report:
            self.assertEqual(app.run_unrar(Path('demo.rar'), Path(DATA.name),
                                          'archive-fixture-secret', 'a'*24), 0)
        percentages = [json.loads(call.kwargs['extraction'])['percent'] for call in report.call_args_list]
        self.assertEqual(percentages[-2:], [99, 100])
        self.assertEqual(percentages, sorted(set(percentages)))
        self.assertTrue(set(percentages) <= {25, 75, 99, 100})

    def test_unrar_failure_never_reports_completion(self):
        real_popen = subprocess.Popen
        script = 'import os,sys; sys.stdin.buffer.read(); os.write(1,b"\\b\\b\\b\\b100%"); sys.exit(3)'
        with patch.object(app.subprocess, 'Popen', side_effect=lambda command, **kwargs:
                          real_popen([sys.executable, '-c', script], **kwargs)), patch.object(app, 'update') as report:
            self.assertEqual(app.run_unrar(Path('demo.rar'), Path(DATA.name), '', 'a'*24), 3)
        self.assertEqual(json.loads(report.call_args.kwargs['extraction'])['percent'], 99)

    def test_extraction_progress_is_exposed_in_authenticated_api(self):
        now = int(time.time())
        with app.connect() as connection:
            connection.execute('INSERT INTO jobs(id,created,updated,state,mode,items,extraction) VALUES(?,?,?,?,?,?,?)',
                               ('b'*24, now, now, 'extracting', 'rar', '[]', '{"percent":42}'))
        self.login()
        self.assertEqual(self.client.get('/api/jobs').json()['jobs'][0]['extraction'], {'percent': 42})


if __name__ == '__main__':
    unittest.main()
