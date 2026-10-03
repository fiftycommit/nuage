"""Rootz transfer QA uses a local HTTP fixture, never a private share link."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import rootz

RAR = b'Rar!\x1a\x07\x01\x00' + b'fixture-content' * 16384


def payload(**changes):
    return {'success': True, 'data': {'fileName': 'fixture.part01.rar',
            'size': len(RAR), 'status': 'active', 'downloadAllowed': True,
            'passwordProtected': False, **changes}}


class FixtureHandler(BaseHTTPRequestHandler):
    body = RAR
    metadata = payload()
    file_requests = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/api/files/download-by-short':
            content, mime = json.dumps(self.metadata).encode(), 'application/json'
        elif self.path == '/file':
            type(self).file_requests += 1
            content, mime = self.body, 'application/octet-stream'
        else:
            content = b'''<button onclick="location.href='/file'">Download</button>
<script>fetch('/api/files/download-by-short')</script>'''
            mime = 'text/html'
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        if self.path == '/file':
            self.send_header('Content-Disposition', 'attachment; filename="fixture.part01.rar"')
        self.end_headers()
        self.wfile.write(content)


class RootzTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/d/fixture'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        FixtureHandler.body = RAR
        FixtureHandler.metadata = payload()
        FixtureHandler.file_requests = 0

    def test_share_allowlist_and_canonical_url(self):
        self.assertEqual(rootz.share_url('https://rootz.so/d/fixture/?unused=yes'),
                         'https://www.rootz.so/d/fixture')
        for url in ('http://rootz.so/d/a', 'https://rootz.so.evil.test/d/a',
                    'https://user:pass@rootz.so/d/a', 'https://rootz.so:443/d/a',
                    'https://rootz.so/api/files/x', 'https://rootz.so/d/../x'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                rootz.share_url(url)

    def test_private_metadata_and_unavailable_files(self):
        for changes in ({'passwordProtected': True}, {'status': 'deleted'},
                        {'size': None}, {'size': True}, {'fileName': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                rootz.file_info(payload(**changes))
        self.assertNotIn('url', rootz.file_info(payload()))

    def test_browser_metadata_does_not_download_file(self):
        with patch.object(rootz, 'share_url', return_value=self.url):
            result = rootz.metadata(self.url)
        self.assertEqual(result['size'], len(RAR))
        self.assertEqual(result['host'], 'Rootz')
        self.assertNotIn('allowed', result)
        self.assertEqual(FixtureHandler.file_requests, 0)

    def test_real_browser_transfer_progress_atomic_completion_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            downloads = Path(temporary) / 'downloads'
            downloads.mkdir()
            target = downloads / 'fixture.part01.rar'
            stale = Path(temporary) / '.rootz-interrupted'
            stale.mkdir()
            (stale / 'partial').write_bytes(b'incomplete previous transfer')
            updates = []
            with patch.object(rootz, 'share_url', return_value=self.url):
                rootz.download({'url': self.url, 'name': target.name, 'size': len(RAR)},
                               target, lambda *values: updates.append(values))
            self.assertEqual(target.read_bytes(), RAR)
            self.assertEqual(updates[0], (0, len(RAR), 0))
            self.assertEqual(updates[-1], (len(RAR), len(RAR), 0))
            self.assertTrue(all(current < len(RAR) for current, _, _ in updates[:-1]))
            self.assertEqual(list(downloads.iterdir()), [target])
            self.assertEqual(list(Path(temporary).iterdir()), [downloads])

    def test_browser_rejects_html_truncated_archive_and_changed_metadata(self):
        for body, changes in ((b'<html>error</html>', {}), (RAR[:-10], {}),
                              (b'<html>' + b'x' * (len(RAR) - 6), {}),
                              (b'x' * len(RAR), {}),
                              (RAR, {'fileName': 'changed.rar'}), (RAR, {'downloadAllowed': False})):
            with self.subTest(changes=changes, length=len(body)), tempfile.TemporaryDirectory() as temporary:
                FixtureHandler.body = body
                FixtureHandler.metadata = payload(**changes)
                downloads = Path(temporary) / 'downloads'
                downloads.mkdir()
                target = downloads / 'fixture.part01.rar'
                with patch.object(rootz, 'share_url', return_value=self.url), self.assertRaises(RuntimeError):
                    rootz.download({'url': self.url, 'name': target.name, 'size': len(RAR)},
                                   target, lambda *values: None)
                self.assertFalse(target.exists())
                self.assertEqual(list(downloads.iterdir()), [])
                self.assertEqual(list(Path(temporary).iterdir()), [downloads])

    def test_progress_uses_actual_bytes_and_does_not_double_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / 'partial').write_bytes(b'x' * 200)
            (folder / 'saved').write_bytes(b'x' * 150)
            self.assertEqual(rootz.sample_progress(folder, 100, 2, 300), (200, 50))
            self.assertEqual(rootz.sample_progress(folder, 300, 1, 100), (100, 0))
