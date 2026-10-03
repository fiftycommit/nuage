"""Render the actual frontend with clearly labelled, synthetic demo data."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--chrome', default='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
args = parser.parse_args()
now = int(time.time())
job_id = '0123456789abcdef01234567'
files = [{'name': name, 'size': size, 'url': f'https://nuage.example/files/{job_id}/{name}'} for name, size in [
    ('Voyage 2026/Film principal.mp4', 12_500_000_000),
    ('Voyage 2026/Photos.zip', 4_280_000_000),
    ('Voyage 2026/Lisez-moi.txt', 1200)]]
ready = {'id': job_id, 'state': 'ready', 'mode': 'rar', 'created': now, 'updated': now,
    'expires_at': now + 5*86400, 'display_name': 'Voyage 2026 · Démo', 'message': '',
    'items': [{'name': 'voyage.part01.rar', 'size': 10_000_000_000, 'host': 'MediaFire'},
              {'name': 'voyage.part02.rar', 'size': 6_000_000_000, 'host': 'MediaFire'}],
    'files': files, 'progress': {}, 'folder_url': f'/job/{job_id}', 'package': {'state': 'none'}}
downloading = {'id': '1123456789abcdef01234567', 'state': 'downloading', 'mode': 'files',
    'created': now, 'updated': now, 'expires_at': 0, 'display_name': 'Archive scientifique · Démo',
    'message': '', 'items': [{'name': 'observations-2026.tar', 'size': 230_000_000_000, 'host': 'MediaFire'}],
    'files': [], 'progress': {'observations-2026.tar': {'completed': 87_400_000_000,
    'total': 230_000_000_000, 'speed': 198_000_000}}, 'package': {'state': 'none'}}
extracting = {'id': '2123456789abcdef01234567', 'state': 'extracting', 'mode': 'rar',
    'created': now, 'updated': now, 'expires_at': 0, 'display_name': 'Collection photo · Démo',
    'message': 'Décompression des archives RAR',
    'items': [{'name': 'photos.part01.rar', 'size': 20_000_000_000, 'host': 'MediaFire'}],
    'files': [], 'progress': {}, 'extraction': {'percent': 42}, 'package': {'state': 'none'}}
mock = '''<script>window.fetch=async(path)=>({ok:true,status:200,json:async()=>
  path==='/api/me'?{authenticated:true}:
  path==='/api/storage'?{total:1080000000000,used:420000000000,free:660000000000,percent_used:38.9}:
  {jobs:JOB_DATA}});</script>'''.replace('JOB_DATA', json.dumps([ready, downloading, extracting]))
base = (ROOT/'index.html').read_text()
output = ROOT/'docs/screenshots'
output.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='nuage-preview-') as tmp:
    preview = Path(tmp)
    shutil.copyfile(ROOT/'file-tiles.js', preview/'file-tiles.js')
    for name, library, width, height in [('add', False, 1440, 1200), ('downloads', True, 1440, 1600), ('downloads-expanded', True, 1440, 1600), ('mobile', True, 390, 1100)]:
        html = base.replace('<script src="/assets/file-tiles.js"></script>', mock+'<script src="file-tiles.js"></script>')
        html = html.replace("const libraryView = location.pathname === '/downloads' || location.pathname.startsWith('/job/');", f'const libraryView = {str(library).lower()};')
        html = html.replace('<div class="eyebrow">Tes fichiers, sans attendre</div>', '<div class="eyebrow">Démonstration · données fictives</div>')
        if name == 'downloads-expanded':
            html = html.replace('const expandedJobs = new Set();', 'const expandedJobs = new Set(' + json.dumps([ready['id'], downloading['id'], extracting['id']]) + ');')
        page = preview/f'{name}.html'
        page.write_text(html)
        subprocess.run([args.chrome, '--headless', '--disable-gpu', '--no-sandbox', '--disable-breakpad', '--no-first-run',
            '--no-default-browser-check', '--hide-scrollbars', '--disable-background-networking',
            '--disable-extensions', '--user-data-dir='+str(preview/'chrome-profile'),
            '--window-size='+f'{width},{height}', '--virtual-time-budget=1800',
            '--screenshot='+str(output/f'{name}.png'), page.as_uri()], check=True,
            stdout=subprocess.DEVNULL, stderr=None, timeout=30)
        print(output/f'{name}.png')
