(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.NuageFileTiles = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  function createFileTile(job, file, context = {}) {
    const doc = context.document || document;
    const store = context.localStorage || localStorage;
    const page = context.location || location;
    const format = context.formatSize || (bytes => String(bytes));
    const onError = context.onError || (() => {});
    const clipboard = context.clipboard || navigator.clipboard;
    const stableFileId = encodeURIComponent(file.name);
    const storageKey = `nuage.file-tile.${job.id}.${stableFileId}`;
    let collapsed = false;
    try { collapsed = store.getItem(storageKey) === 'collapsed'; } catch (_) {}

    const tile = doc.createElement('article');
    tile.className = 'file-tile';
    const head = doc.createElement('div');
    head.className = 'file-tile-head';
    const info = doc.createElement('div');
    info.className = 'file-tile-info';
    const title = doc.createElement('strong');
    title.className = 'file-tile-name';
    title.textContent = file.name;
    const size = doc.createElement('small');
    size.className = 'file-tile-size';
    size.textContent = format(file.size);
    info.append(title, size);

    const download = doc.createElement('a');
    download.className = 'file-download';
    download.href = file.url;
    download.download = file.name.split('/').pop();
    download.textContent = '↓ Télécharger';

    const bodyId = `file-details-${job.id}-${stableFileId}`;
    const toggle = doc.createElement('button');
    toggle.type = 'button';
    toggle.className = 'tile-toggle';
    toggle.textContent = collapsed ? '+' : '−';
    toggle.setAttribute('aria-expanded', String(!collapsed));
    toggle.setAttribute('aria-controls', bodyId);
    toggle.setAttribute('aria-label', `${collapsed ? 'Déplier' : 'Réduire'} ${file.name}`);

    const body = doc.createElement('div');
    body.id = bodyId;
    body.className = 'file-tile-body';
    body.hidden = collapsed;
    const label = doc.createElement('span');
    label.className = 'file-tile-link-label';
    label.textContent = 'Lien direct';
    const url = doc.createElement('code');
    url.className = 'file-tile-url';
    url.textContent = new URL(file.url, page.href).href;
    const copy = doc.createElement('button');
    copy.type = 'button';
    copy.className = 'file-copy';
    copy.textContent = 'Copier le lien';
    copy.addEventListener('click', async () => {
      try {
        await clipboard.writeText(url.textContent);
        copy.textContent = 'Lien copié ✓';
      } catch (_) {
        onError('Impossible de copier automatiquement le lien.', true);
      }
    });
    body.append(label, url, copy);

    toggle.addEventListener('click', () => {
      collapsed = !collapsed;
      body.hidden = collapsed;
      toggle.textContent = collapsed ? '+' : '−';
      toggle.setAttribute('aria-expanded', String(!collapsed));
      toggle.setAttribute('aria-label', `${collapsed ? 'Déplier' : 'Réduire'} ${file.name}`);
      try { store.setItem(storageKey, collapsed ? 'collapsed' : 'expanded'); } catch (_) {}
    });
    head.append(info, download, toggle);
    tile.append(head, body);
    return tile;
  }
  return {createFileTile};
});
