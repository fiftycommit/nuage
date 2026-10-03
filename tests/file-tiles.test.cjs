const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createFileTile, createJobTile} = require('../file-tiles.js');

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.style = {};
    this.hidden = false;
    this.textContent = '';
  }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  click() { return this.listeners.click?.(); }
  querySelector(className) {
    const wanted = className.replace('.', '');
    const visit = element => {
      if ((element.className || '').split(/\s+/).includes(wanted)) return element;
      for (const child of element.children) {
        const match = visit(child);
        if (match) return match;
      }
      return null;
    };
    return visit(this);
  }
}

function setup() {
  const values = new Map();
  const document = {createElement: tag => new FakeElement(tag)};
  const localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value))
  };
  const context = {
    document,
    localStorage,
    location: {href: 'https://nuage.example/downloads'},
    formatSize: size => `${size} octets`,
    clipboard: {writeText: async value => { context.copied = value; }},
    onError: message => { context.error = message; }
  };
  return {context, values};
}

const job = {id: 'abc123'};
const file = {name: 'dossier/fichier.bin', size: 1234, url: '/files/abc123/fichier.bin'};

test('file cards keep their actions and no longer have individual toggles', async () => {
  const {context, values} = setup();
  values.set('nuage.file-tile.abc123.dossier%2Ffichier.bin', 'collapsed');
  const tile = createFileTile(job, file, context);
  assert.equal(tile.querySelector('.tile-toggle'), null);
  assert.equal(tile.querySelector('.file-tile-body').hidden, false);
  assert.equal(tile.querySelector('.file-download').href, file.url);
  assert.equal(tile.querySelector('.file-download').download, 'fichier.bin');
  await tile.querySelector('.file-copy').click();
  assert.equal(context.copied, 'https://nuage.example/files/abc123/fichier.bin');
});

function jobTileSetup(expandedJobs = new Set(), id = 'abc123') {
  const {context} = setup();
  const header = context.document.createElement('div');
  const tile = createJobTile({id,display_name:'Mon téléchargement'}, header, {...context,expandedJobs});
  const body = tile.querySelector('.job-tile-body');
  for (const name of ['progress-card', 'retry', 'folder-actions', 'bundle-note', 'filelist']) {
    const child = context.document.createElement('div');
    child.className = name;
    body.append(child);
  }
  return {tile,header,body,toggle:tile.querySelector('.tile-toggle'),expandedJobs};
}

test('download tile starts closed and encloses all related content', () => {
  const {tile,body,toggle} = jobTileSetup();
  assert.equal(body.hidden, true);
  assert.equal(toggle.tagName, 'BUTTON');
  assert.equal(toggle.textContent, '+');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(toggle.getAttribute('aria-controls'), body.id);
  for (const name of ['progress-card','retry','folder-actions','bundle-note','filelist']) {
    assert.equal(tile.querySelector('.' + name), body.querySelector('.' + name));
  }
});

test('download toggle hides the entire body and retains its state during polling only', () => {
  const first = jobTileSetup();
  first.toggle.click();
  assert.equal(first.body.hidden, false);
  assert.equal(first.toggle.textContent, '−');
  assert.equal(first.toggle.getAttribute('aria-expanded'), 'true');
  const refreshed = jobTileSetup(first.expandedJobs);
  assert.equal(refreshed.body.hidden, false);
  assert.equal(jobTileSetup(first.expandedJobs, 'other').body.hidden, true);
  assert.equal(jobTileSetup().body.hidden, true);
  refreshed.toggle.click();
  assert.equal(refreshed.body.hidden, true);
  assert.equal(refreshed.toggle.getAttribute('aria-expanded'), 'false');
});

test('clicking the header toggles the download while action buttons leave it alone', () => {
  const {header,body} = jobTileSetup();
  header.listeners.click({target:{closest: () => null}});
  assert.equal(body.hidden, false);
  header.listeners.click({target:{closest: () => ({})}});
  assert.equal(body.hidden, false);
});

test('toggle styling has no decorative border, fill, shadow, or circular shape', () => {
  const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  const rule = html.match(/\.tile-toggle\s*\{([^}]*)\}/)?.[1];
  assert.ok(rule, 'tile toggle CSS rule exists');
  assert.match(rule, /border:\s*0/);
  assert.match(rule, /background:\s*transparent/);
  assert.match(rule, /border-radius:\s*0/);
  assert.match(rule, /box-shadow:\s*none/);
  assert.match(html, /\.tile-toggle:hover\s*\{[^}]*background:\s*transparent/);
  assert.match(html, /\.tile-toggle:focus-visible\s*\{[^}]*outline:/);
});

test('page loads the reusable tile component and its inline script parses', () => {
  const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  const inlineScripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
    .map(match => match[1]);
  assert.match(html, /<script src="\/assets\/file-tiles\.js"><\/script>/);
  assert.match(html, /const \{createFileTile, createJobTile\} = window\.NuageFileTiles/);
  assert.match(html, /for \(const file of visible\) files\.append\(createFileTile/);
  assert.match(html, /\.job-tile-body\[hidden\]\s*\{\s*display:\s*none/);
  assert.doesNotMatch(html, /box\.append\((?:progressCard|form|actions|note|packaging|error|files)/);
  assert.ok(inlineScripts.length > 0);
  assert.doesNotThrow(() => new Function(inlineScripts.at(-1)));

  const app = fs.readFileSync(path.join(__dirname, '..', 'app.py'), 'utf8');
  assert.match(app, /@app\.get\("\/assets\/file-tiles\.js"\)/);
  assert.match(app, /FileResponse\(HERE \/ "file-tiles\.js", media_type="application\/javascript"\)/);
});

test('extraction has its own percentage and handles unknown and failed progress', () => {
  const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  const source = html.slice(html.indexOf('function extractionProgressCard(job)'), html.indexOf('async function loadJobs'));
  const {context} = setup();
  const sandbox = {document: context.document};
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  const render = job => sandbox.progressCard({items: [{name:'demo.part01.rar',size:100}],
    progress:{'demo.part01.rar':{completed:100,total:100,speed:500}}, ...job});
  const card = render({state:'extracting', extraction:{percent:42}});
  assert.equal(card.children[0].children[1].textContent, '42 % décompressé · 58 % restant');
  assert.equal(card.children[1].getAttribute('aria-valuenow'), '42');
  assert.equal(card.children[1].children[0].style.width, '42%');
  const pending = render({state:'extracting', extraction:{percent:null}});
  assert.equal(pending.children[1].getAttribute('aria-valuenow'), null);
  assert.match(pending.children[0].children[1].textContent, /progression en attente/);
  const failed = render({state:'failed', extraction:{percent:42}});
  assert.equal(failed.children[0].children[0].textContent, 'Décompression interrompue');
});

function deletionSetup(confirmed = true) {
  const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  const source = html.slice(html.indexOf('let deletingJob = false;'), html.indexOf('function extractionProgressCard(job)'));
  const {context} = setup();
  const calls = [];
  const sandbox = {document: context.document, window:{confirm: message => {calls.push(['confirm',message]); return confirmed;}},
    api: async (...args) => {calls.push(['api',...args]);},
    showMessage: (...args) => {calls.push(['message',...args]);},
    loadJobs: async force => {calls.push(['reload',force]);},
    location:{assign: path => {calls.push(['redirect',path]);}}};
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return {sandbox,calls,job:{id:'demo',display_name:'Mon dossier',state:'ready',package:{state:'none'}}};
}

test('delete button confirms permanent removal then refreshes the library', async () => {
  const {sandbox,calls,job} = deletionSetup();
  const button = sandbox.deleteJobButton(job, null);
  assert.equal(button.getAttribute('aria-label'), 'Supprimer Mon dossier');
  await button.click();
  assert.match(calls[0][1], /définitivement effacés/);
  assert.equal(calls[1][1], '/api/jobs/demo');
  assert.equal(calls[1][2].method, 'DELETE');
  assert.ok(calls.some(call => call[0] === 'reload' && call[1] === true));
});

test('delete cancellation makes no request and deleting a folder page returns to the library', async () => {
  const cancelled = deletionSetup(false);
  await cancelled.sandbox.deleteJobButton(cancelled.job, null).click();
  assert.equal(cancelled.calls.length, 1);
  const selected = deletionSetup();
  await selected.sandbox.deleteJobButton(selected.job, selected.job.id).click();
  assert.ok(selected.calls.some(call => call[0] === 'redirect' && call[1] === '/downloads'));
});

test('delete is disabled during transfers, extraction and ZIP creation', () => {
  const {sandbox,job} = deletionSetup();
  for (const state of ['downloading','extracting','publishing']) {
    assert.equal(sandbox.deleteJobButton({...job,state},null).disabled, true);
  }
  assert.equal(sandbox.deleteJobButton({...job,package:{state:'building'}},null).disabled, true);
  assert.equal(sandbox.deleteJobButton({...job,state:'queued'},null).disabled, false);
});
