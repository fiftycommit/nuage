"""Rootz's normal browser download flow; no challenge solving or stealth browser."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable

from playwright.async_api import Error as BrowserError, async_playwright

# Deployment keeps the browser matching this virtualenv beside its dependencies.
_browser_cache = Path(sys.prefix) / 'share' / 'browsers'
if _browser_cache.is_dir():
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(_browser_cache))
_download_lock = threading.Lock()
_probe_slots = threading.BoundedSemaphore(2)


def share_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ('rootz.so', 'www.rootz.so')
            or parsed.username or parsed.password or parsed.port
            or not re.fullmatch(r'/d/[A-Za-z0-9_-]{1,80}/?', parsed.path)):
        raise ValueError('Utilise un lien de partage Rootz https://www.rootz.so/d/…')
    return 'https://www.rootz.so' + parsed.path.rstrip('/')


def file_info(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError('Réponse Rootz invalide')
    data = payload.get('data')
    if not payload.get('success') or not isinstance(data, dict) or data.get('status') != 'active':
        raise ValueError('Fichier Rootz indisponible')
    if data.get('passwordProtected'):
        raise ValueError('Ce lien Rootz demande un mot de passe sur son site')
    if not isinstance(data.get('fileName'), str) or not data['fileName']:
        raise ValueError('Rootz ne fournit pas de nom de fichier')
    size = data.get('size')
    if type(size) is not int or size <= 0:
        raise ValueError('Rootz ne fournit pas de taille de fichier valide')
    return {'name': data['fileName'], 'size': size, 'mime': data.get('mimeType'),
            'host': 'Rootz', 'note': 'Lien Rootz vérifié',
            'cooldown': max(0, int(data.get('cooldownRemaining') or 0)),
            'allowed': data.get('downloadAllowed') is True}


async def page_info(page, url: str) -> dict:
    def metadata_response(response):
        return urllib.parse.urlsplit(response.url).path == '/api/files/download-by-short'
    async with page.expect_response(metadata_response, timeout=20000) as event:
        response = await page.goto(share_url(url), wait_until='domcontentloaded', timeout=20000)
        if response is None or response.status != 200:
            raise ValueError('Rootz refuse l’accès : ouvre le lien sur son site')
    metadata = await event.value
    if metadata.status != 200:
        raise ValueError('Rootz demande une vérification sur son site')
    return file_info(await metadata.json())


async def _probe(url: str) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(accept_downloads=False)
            info = await page_info(await context.new_page(), url)
            if not info['allowed']:
                raise ValueError('Rootz refuse ce téléchargement ; ouvre le lien sur son site')
            return {key: value for key, value in info.items() if key not in ('cooldown', 'allowed')}
        finally:
            await browser.close()


def metadata(url: str) -> dict:
    url = share_url(url)
    if not _probe_slots.acquire(timeout=5):
        raise ValueError('Vérification Rootz occupée ; réessaie dans quelques secondes')
    try:
        return {**asyncio.run(_probe(url)), 'url': url}
    except BrowserError:
        raise ValueError('Rootz inaccessible ou vérification humaine requise sur son site') from None
    finally:
        _probe_slots.release()


def sample_progress(directory: Path, previous: int, elapsed: float, total: int) -> tuple[int, int]:
    # Chromium may rename its partial file as the download finishes.
    sizes = []
    for path in directory.iterdir():
        try:
            if path.is_file():
                sizes.append(path.stat().st_size)
        except FileNotFoundError:
            pass  # Chromium renamed its partial file between observations.
    completed = max(sizes, default=0)
    completed = min(completed, total)
    return completed, max(0, int((completed - previous) / max(elapsed, 0.001)))


def validate_file(path: Path, size: int, name: str) -> None:
    if not path.is_file() or path.stat().st_size != size:
        raise RuntimeError('Taille du téléchargement Rootz incorrecte')
    with path.open('rb') as stream:
        signature = stream.read(512)
    if signature.lstrip().lower().startswith((b'<!doctype html', b'<html')):
        raise RuntimeError('Rootz a renvoyé une page au lieu du fichier')
    if name.lower().endswith('.rar') and not signature.startswith(b'Rar!\x1a\x07'):
        raise RuntimeError('Le téléchargement Rootz ne contient pas une archive RAR valide')


async def _download(item: dict, target: Path, progress: Callable[[int, int, int], None]) -> None:
    # Partial browser downloads stay isolated and are deleted on failure/restart.
    workspace = target.parent.parent
    # This directory is outside downloads/extracted, so a crash cannot publish a partial.
    for stale in workspace.glob('.rootz-*'):
        if stale.is_symlink():
            stale.unlink()
        elif stale.is_dir():
            shutil.rmtree(stale)
    with tempfile.TemporaryDirectory(prefix='.rootz-', dir=workspace) as temporary:
        staging = Path(temporary)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, downloads_path=str(staging))
            try:
                context = await browser.new_context(accept_downloads=True)
                page = await context.new_page()
                info = await page_info(page, item['url'])
                if info['name'] != item['name'] or info['size'] != item['size']:
                    raise RuntimeError('Le fichier Rootz a changé depuis sa vérification')
                if not info['allowed']:
                    raise RuntimeError('Rootz refuse ce téléchargement ; ouvre le lien sur son site')
                if info['cooldown'] > 60:
                    raise RuntimeError('Rootz demande d’attendre avant un nouveau téléchargement')
                if info['cooldown']:
                    await asyncio.sleep(info['cooldown'])
                progress(0, info['size'], 0)
                async with page.expect_download(timeout=30000) as event:
                    await page.get_by_role('button', name='Download', exact=True).click(timeout=15000)
                download = await event.value
                saving = asyncio.create_task(download.path())
                previous, last = 0, time.monotonic()
                try:
                    # Time limit covers very large files; no fixed short download timeout.
                    deadline = last + 24 * 60 * 60
                    while not saving.done():
                        await asyncio.wait({saving}, timeout=1)
                        now = time.monotonic()
                        current, speed = sample_progress(staging, previous, now - last, info['size'])
                        # Reserve completion for successful size/signature validation.
                        progress(min(current, info['size'] - 1), info['size'], speed)
                        previous, last = current, now
                        if now >= deadline:
                            await download.cancel()
                            raise RuntimeError('Le téléchargement Rootz a dépassé 24 heures')
                    saved = Path(await saving)
                    validate_file(saved, info['size'], target.name)
                    os.replace(saved, target)
                    progress(info['size'], info['size'], 0)
                finally:
                    if not saving.done():
                        saving.cancel()
                    await asyncio.gather(saving, return_exceptions=True)
            finally:
                await browser.close()


def download(item: dict, target: Path, progress: Callable[[int, int, int], None]) -> None:
    # Rootz free downloads are sequential, including multipart archives.
    with _download_lock:
        try:
            asyncio.run(_download(item, target, progress))
        except BrowserError:
            raise RuntimeError('Téléchargement Rootz échoué ou vérification humaine requise sur son site') from None
