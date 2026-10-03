"""Filekeeper QA uses local fixtures; private source links never enter the repository."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch

import filekeeper


class FixtureHandler(BaseHTTPRequestHandler):
    protected = False
    missing = False
    requests = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/attachment':
            body = b'7z\xbc\xaf\x27\x1c' + b'fixture' * 1024
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Content-Disposition', 'attachment; filename="fixture.7z"')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.missing:
            content = b'<h1>File Not Found</h1>'
        else:
            content = ('''<h1 id="dl-filename">fixture.7z</h1>
<div id="download-countdown" data-has-password="%s" data-has-captcha="false"></div>
<script>setTimeout(()=>{let b=document.createElement('button');b.id='download-button';
b.textContent='Free download';b.onclick=()=>{let f=document.createElement('form');
f.method='POST';f.action='/file';document.body.append(f);f.submit()};
document.getElementById('download-countdown').append(b)},100)</script>'''
                       % ('true' if self.protected else 'false')).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        type(self).requests += 1
        self.send_response(302)
        self.send_header('Location', '/attachment')
        self.end_headers()

class FilekeeperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/fixture'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        FixtureHandler.protected = False
        FixtureHandler.missing = False
        FixtureHandler.requests = 0

    def test_share_and_direct_allowlists(self):
        self.assertEqual(filekeeper.share_url('https://www.filekeeper.net/abcdef123456/'),
                         'https://filekeeper.net/abcdef123456')
        for url in ('http://filekeeper.net/abcdef123456', 'https://filekeeper.net/download#',
                    'https://filekeeper.net.evil.test/abcdef123456',
                    'https://u:p@filekeeper.net/abcdef123456',
                    'https://filekeeper.net:443/abcdef123456'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                filekeeper.share_url(url)
        self.assertEqual(filekeeper.check_direct('https://tunnel1.dlproxy.uk/file'),
                         'https://tunnel1.dlproxy.uk/file')
        for url in ('https://127.0.0.1/file', 'https://tunnel1.dlproxy.uk.evil.test/file',
                    'http://tunnel1.dlproxy.uk/file', 'https://u:p@tunnel1.dlproxy.uk/file'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                filekeeper.check_direct(url)

    def test_real_browser_waits_for_button_submits_form_and_resolves_attachment(self):
        import asyncio
        with patch.object(filekeeper, 'share_url', return_value=self.url), patch.object(
                filekeeper, 'check_direct', side_effect=lambda url: url):
            name, direct = asyncio.run(filekeeper.browser_link(self.url))
        self.assertEqual(name, 'fixture.7z')
        self.assertEqual(direct, self.url.rsplit('/', 1)[0] + '/attachment')
        self.assertEqual(FixtureHandler.requests, 1)

    def test_browser_refuses_missing_and_protected_files_before_submission(self):
        import asyncio
        for flag in ('missing', 'protected'):
            with self.subTest(flag=flag):
                self.setUp()
                setattr(FixtureHandler, flag, True)
                with patch.object(filekeeper, 'share_url', return_value=self.url), self.assertRaises(ValueError):
                    asyncio.run(filekeeper.browser_link(self.url))
                self.assertEqual(FixtureHandler.requests, 0)

    def test_metadata_never_exposes_temporary_direct_link(self):
        with patch.object(filekeeper, 'resolve', return_value={
                'name': 'fixture.7z', 'size': 100, 'direct': 'https://tunnel1.dlproxy.uk/private-token'}):
            result = filekeeper.metadata('https://filekeeper.net/abcdef123456')
        self.assertNotIn('direct', result)

    def test_head_verifies_exact_size_and_refuses_html(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        for headers, valid in (({'Content-Length': '96793000001'}, True),
                               ({'Content-Length': '0'}, False),
                               ({'Content-Length': '100', 'Content-Type': 'text/html'}, False),
                               ({}, False)):
            response = SimpleNamespace(status=200, url='https://tunnel1.dlproxy.uk/file', headers=headers)
            context = MagicMock()
            context.__enter__.return_value = response
            with self.subTest(headers=headers), patch.object(filekeeper.urllib.request, 'build_opener') as opener:
                opener.return_value.open.return_value = context
                if valid:
                    self.assertEqual(filekeeper.direct_info(response.url)['size'], 96793000001)
                    request = opener.return_value.open.call_args.args[0]
                    self.assertEqual(request.get_method(), 'HEAD')
                else:
                    with self.assertRaises(ValueError):
                        filekeeper.direct_info(response.url)
