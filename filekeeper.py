"""Filekeeper's normal countdown/button flow, then a validated direct URL for aria2."""
from __future__ import annotations

import asyncio
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from playwright.async_api import Error as BrowserError, async_playwright

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
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            # Only obtain the destination: Chromium cancels the attachment immediately.
            context = await browser.new_context(accept_downloads=False)
            page = await context.new_page()
            response = await page.goto(share_url(url), wait_until='domcontentloaded', timeout=15000)
            if response is None or response.status != 200:
                raise ValueError('Fichier Filekeeper indisponible')
            filename = page.locator('#dl-filename')
            if not await filename.count():
                raise ValueError('Fichier Filekeeper introuvable ou vérification humaine requise')
            name = (await filename.inner_text()).strip()
            if not name or '/' in name or '\\' in name or any(ord(char) < 32 for char in name):
                raise ValueError('Nom de fichier Filekeeper invalide')
            countdown = page.locator('#download-countdown')
            if not await countdown.count():
                raise ValueError('Parcours de téléchargement Filekeeper non reconnu')
            if (await countdown.get_attribute('data-has-password') == 'true'
                    or await countdown.get_attribute('data-has-captcha') == 'true'):
                raise ValueError('Termine la vérification ou le mot de passe sur Filekeeper')
            async with page.expect_download(timeout=15000) as event:
                # Waiting for the real button honours the site's countdown.
                await page.locator('#download-button').click(timeout=12000)
            download = await event.value
            direct = check_direct(download.url)
            await download.cancel()
            return name, direct
        finally:
            await browser.close()


def resolve(url: str) -> dict:
    url = share_url(url)
    if not _slots.acquire(timeout=10):
        raise ValueError('Vérification Filekeeper occupée ; réessaie dans quelques secondes')
    try:
        name, direct = asyncio.run(browser_link(url))
        return {'name': name, 'host': 'Filekeeper', 'url': url,
                'note': 'Lien Filekeeper vérifié', **direct_info(direct)}
    except BrowserError:
        raise ValueError('Filekeeper inaccessible ou vérification humaine requise sur son site') from None
    finally:
        _slots.release()


def metadata(url: str) -> dict:
    # The temporary direct URL stays private and is regenerated when the worker starts.
    return {key: value for key, value in resolve(url).items() if key != 'direct'}
