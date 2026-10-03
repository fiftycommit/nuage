const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {createFileTile} = require('../file-tiles.js');

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.listeners = {};
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

test('file card is open by default and exposes an accessible minus control', () => {
  const {context} = setup();
  const tile = createFileTile(job, file, context);
  const toggle = tile.querySelector('.tile-toggle');
  const body = tile.querySelector('.file-tile-body');
  const download = tile.querySelector('.file-download');

  assert.equal(toggle.textContent, '−');
  assert.equal(toggle.tagName, 'BUTTON');
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(toggle.getAttribute('aria-controls'), body.id);
  assert.equal(toggle.getAttribute('aria-label'), 'Réduire dossier/fichier.bin');
  assert.equal(body.hidden, false);
  assert.equal(download.href, file.url);
  assert.equal(tile.querySelector('.file-tile-size').textContent, '1234 octets');
});

test('toggle collapses and expands the card, updates ARIA, and persists per file', () => {
  const {context, values} = setup();
  const tile = createFileTile(job, file, context);
  const toggle = tile.querySelector('.tile-toggle');
  const body = tile.querySelector('.file-tile-body');

  toggle.click();
  assert.equal(body.hidden, true);
  assert.equal(toggle.textContent, '+');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(toggle.getAttribute('aria-label'), 'Déplier dossier/fichier.bin');
  assert.equal(values.get('nuage.file-tile.abc123.dossier%2Ffichier.bin'), 'collapsed');

  const restored = createFileTile(job, file, context);
  assert.equal(restored.querySelector('.file-tile-body').hidden, true);
  assert.equal(restored.querySelector('.tile-toggle').textContent, '+');

  restored.querySelector('.tile-toggle').click();
  assert.equal(restored.querySelector('.file-tile-body').hidden, false);
  assert.equal(restored.querySelector('.tile-toggle').textContent, '−');
  assert.equal(values.get('nuage.file-tile.abc123.dossier%2Ffichier.bin'), 'expanded');
});

test('different files have independent state and retain download and copy actions', async () => {
  const {context, values} = setup();
  const first = createFileTile(job, file, context);
  const second = createFileTile(job, {...file, name: 'autre.bin'}, context);
  first.querySelector('.tile-toggle').click();

  assert.equal(first.querySelector('.file-tile-body').hidden, true);
  assert.equal(second.querySelector('.file-tile-body').hidden, false);
  assert.equal(values.get('nuage.file-tile.abc123.dossier%2Ffichier.bin'), 'collapsed');
  assert.equal(values.has('nuage.file-tile.abc123.autre.bin'), false);
  assert.equal(first.querySelector('.file-download').download, 'fichier.bin');
  await first.querySelector('.file-copy').click();
  assert.equal(context.copied, 'https://nuage.example/files/abc123/fichier.bin');
  assert.equal(first.querySelector('.file-copy').textContent, 'Lien copié ✓');
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
  assert.match(html, /const \{createFileTile\} = window\.NuageFileTiles/);
  assert.match(html, /for \(const file of visible\) files\.append\(createFileTile/);
  assert.ok(inlineScripts.length > 0);
  assert.doesNotThrow(() => new Function(inlineScripts.at(-1)));

  const app = fs.readFileSync(path.join(__dirname, '..', 'app.py'), 'utf8');
  assert.match(app, /@app\.get\("\/assets\/file-tiles\.js"\)/);
  assert.match(app, /FileResponse\(HERE \/ "file-tiles\.js", media_type="application\/javascript"\)/);
});
