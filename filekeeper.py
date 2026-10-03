"""Filekeeper's normal countdown/button flow, then a validated direct URL for aria2."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import signal
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

from playwright.async_api import async_playwright

import rootz  # Shares the deployed Playwright browser cache configuration.

_slots = threading.BoundedSemaphore(2)


def share_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ('filekeeper.net', 'www.filekeeper.net')
            or parsed.username or parsed.password or parsed.port
            or not re.fullmatch(r'/[a-z0-9]{12}/?', parsed.path)):
        raise ValueError('Utilise le lien Filekeeper du fichier, avec son identifiant après /')
    return 'https://filekeeper.net' + parsed.path.rstrip('/')


def check_direct(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port
            or not (host in ('filekeeper.net', 'www.filekeeper.net')
                    or re.fullmatch(r'tunnel\d+\.dlproxy\.uk', host))):
        raise ValueError('Filekeeper a renvoyé une destination de téléchargement non reconnue')
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        check_direct(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def direct_info(url: str) -> dict:
    check_direct(url)
    request = urllib.request.Request(url, method='HEAD')
    try:
        with urllib.request.build_opener(SafeRedirect()).open(request, timeout=15) as response:
            check_direct(response.url)
            if response.status != 200:
                raise ValueError('Lien de téléchargement Filekeeper indisponible')
            mime = response.headers.get('Content-Type', '').lower()
            if 'text/html' in mime:
                raise ValueError('Filekeeper a renvoyé une page au lieu du fichier')
            size = response.headers.get('Content-Length', '')
            if not size.isdigit() or int(size) <= 0:
                raise ValueError('Filekeeper ne fournit pas de taille exacte')
            return {'size': int(size), 'mime': mime or None, 'direct': response.url}
    except urllib.error.URLError:
        raise ValueError('Lien de téléchargement Filekeeper inaccessible') from None


async def browser_link(url: str) -> tuple[str, str]:
    # Use the full, versioned Chromium installed alongside Playwright for Rootz.
    async with async_playwright() as p:
        chrome = p.chromium.executable_path
    url = share_url(url)
    helper = Path(__file__).resolve().parent / 'scripts' / 'filekeeper-browser.cjs'
    profile = tempfile.TemporaryDirectory(prefix='nuage-filekeeper-')
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            'node', str(helper), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, 'NUAGE_CHROME_PATH': chrome,
                 'NUAGE_BROWSER_PROFILE': profile.name}, start_new_session=True)
        stdout, _ = await process.communicate(json.dumps({'url': url}).encode())
        # The library can write startup diagnostics before our final JSON line.
        data = json.loads(stdout.decode().strip().splitlines()[-1])
        if process.returncode or 'error' in data:
            raise ValueError(data.get('error', 'Navigateur Filekeeper indisponible'))
        return data['name'], check_direct(data['direct'])
    except OSError:
        raise ValueError('Navigateur Filekeeper indisponible') from None
    except (IndexError, KeyError, json.JSONDecodeError):
        raise ValueError('Réponse du navigateur Filekeeper invalide') from None
    finally:
        # Chrome launcher creates detached process groups; cancellation must reap
        # those too, but only processes carrying this request's unique profile.
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        reap_browser_profile(profile.name)
        profile.cleanup()


def reap_browser_profile(profile: str):
    for entry in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args = entry.read_bytes().split(b'\0')
            if any(arg == f'--user-data-dir={profile}'.encode()
                   or arg.startswith(f'--database={profile}/'.encode()) for arg in args):
                os.kill(int(entry.parent.name), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def resolve(url: str) -> dict:
    url = share_url(url)
    if not _slots.acquire(timeout=10):
        raise ValueError('Vérification Filekeeper occupée ; réessaie dans quelques secondes')
    try:
        name, direct = asyncio.run(asyncio.wait_for(browser_link(url), timeout=45))
        return {'name': name, 'host': 'Filekeeper', 'url': url,
                'note': 'Lien Filekeeper vérifié', **direct_info(direct)}
    except asyncio.TimeoutError:
        raise ValueError('Filekeeper n’a pas répondu à temps ; réessaie la vérification') from None
    finally:
        _slots.release()


def metadata(url: str) -> dict:
    # The temporary direct URL stays private and is regenerated when the worker starts.
    return {key: value for key, value in resolve(url).items() if key != 'direct'}
