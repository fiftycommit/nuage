// Only resolve the attachment headers; aria2 downloads the file afterwards.
const { connect } = require('puppeteer-real-browser');
const { mkdtemp, rm } = require('node:fs/promises');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function resolveLink(url) {
  const profile = process.env.NUAGE_BROWSER_PROFILE || await mkdtemp(join(tmpdir(), 'nuage-filekeeper-'));
  // The service account has no home directory; Chrome needs writable config/cache.
  process.env.XDG_CONFIG_HOME = profile;
  process.env.XDG_CACHE_HOME = profile;
  let browser;
  try {
    const connected = await connect({
      headless: process.platform === 'linux' ? false : 'new',
      disableXvfb: process.platform !== 'linux',
      turnstile: false,
      customConfig: {chromePath: process.env.NUAGE_CHROME_PATH, userDataDir: profile},
      connectOption: {defaultViewport: null},
    });
    browser = connected.browser;
    const page = connected.page;
    const response = await page.goto(url, {waitUntil: 'domcontentloaded', timeout: 15000});
    if (!response || response.status() !== 200) throw new Error('Fichier Filekeeper indisponible');
    const filename = await page.$('#dl-filename');
    if (!filename) throw new Error('Fichier Filekeeper introuvable ou vérification humaine requise');
    const name = (await filename.evaluate(el => el.textContent)).trim();
    if (!name || /[/\\\x00-\x1f]/.test(name)) throw new Error('Nom de fichier Filekeeper invalide');
    const countdown = await page.$('#download-countdown');
    if (!countdown) throw new Error('Parcours de téléchargement Filekeeper non reconnu');
    const protectedFile = await countdown.evaluate(el =>
      el.dataset.hasPassword === 'true' || el.dataset.hasCaptcha === 'true');
    if (protectedFile) throw new Error('Termine la vérification ou le mot de passe sur Filekeeper');

    const session = await page.createCDPSession();
    let direct;
    let submitted = false;
    const origin = new URL(page.url()).origin;
    page.on('request', request => {
      if (request.method() === 'POST' && new URL(request.url()).origin === origin) submitted = true;
    });
    await session.send('Fetch.enable', {patterns: [{urlPattern: '*', requestStage: 'Response'}]});
    session.on('Fetch.requestPaused', async event => {
      try {
        const attachment = event.responseHeaders?.some(header =>
          header.name.toLowerCase() === 'content-disposition' && /attachment/i.test(header.value));
        if (attachment && event.responseStatusCode === 200) {
          direct = event.request.url;
          await session.send('Fetch.failRequest', {requestId: event.requestId, errorReason: 'Aborted'});
        } else {
          await session.send('Fetch.continueRequest', {requestId: event.requestId});
        }
      } catch { /* Closing the browser can cancel paused requests. */ }
    });
    await page.waitForSelector('#download-button', {visible: true, timeout: 12000});
    await page.$eval('#download-button', el => el.scrollIntoView({block: 'center'}));
    await page.realCursor.click('#download-button');
    const deadline = Date.now() + 18000;
    let nextFallback = Date.now() + 2000;
    let retries = 0;
    while (!direct && Date.now() < deadline) {
      await delay(100);
      if (!submitted && Date.now() >= nextFallback && retries < 2) {
        if (new URL(page.url()).origin !== origin) throw new Error('Filekeeper n’a pas lancé le téléchargement');
        const fallback = await page.$('#download-link');
        if (!fallback) throw new Error('Filekeeper n’a pas lancé le téléchargement ; réessaie');
        // Ad tabs may never finish initializing; close them through CDP directly.
        const {targetInfo: current} = await session.send('Target.getTargetInfo');
        const {targetInfos} = await session.send('Target.getTargets');
        for (const target of targetInfos) {
          if (target.type === 'page' && target.targetId !== current.targetId) {
            await session.send('Target.closeTarget', {targetId: target.targetId});
          }
        }
        await fallback.evaluate(el => el.scrollIntoView({block: 'center'}));
        await page.realCursor.click('#download-link');
        retries++;
        nextFallback = Date.now() + 2000;
      }
    }
    if (!direct) throw new Error('Filekeeper n’a pas répondu à temps ; réessaie la vérification');
    return {name, direct};
  } finally {
    if (browser) await browser.close();
    await rm(profile, {recursive: true, force: true});
  }
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', data => { input += data; });
process.stdin.on('end', async () => {
  try {
    const {url} = JSON.parse(input);
    const result = await resolveLink(url);
    process.stdout.write(JSON.stringify(result) + '\n');
  } catch (error) {
    const message = error.name === 'TimeoutError'
      ? 'Filekeeper n’a pas répondu à temps ; réessaie la vérification'
      : error.message.startsWith('Filekeeper') || error.message.startsWith('Fichier')
        || error.message.startsWith('Nom de fichier') || error.message.startsWith('Parcours')
        || error.message.startsWith('Termine') ? error.message : 'Navigateur Filekeeper indisponible';
    process.stdout.write(JSON.stringify({error: message}) + '\n');
    process.exitCode = 1;
  }
});
