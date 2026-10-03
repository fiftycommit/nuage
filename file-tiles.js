(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.NuageFileTiles = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  function createFileTile(job, file, context = {}) {
    const doc = context.document || document;
    const page = context.location || location;
    const format = context.formatSize || (bytes => String(bytes));
    const onError = context.onError || (() => {});
    const clipboard = context.clipboard || navigator.clipboard;
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

    const body = doc.createElement('div');
    body.className = 'file-tile-body';
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

    head.append(info, download);
    tile.append(head, body);
    return tile;
  }
  function createJobTile(job, header, context = {}) {
    const doc = context.document || document;
    const expanded = context.expandedJobs || new Set();
    const tile = doc.createElement('article');
    tile.className = 'job-tile';
    const body = doc.createElement('div');
    body.className = 'job-tile-body';
    body.id = `job-details-${job.id}`;
    const toggle = doc.createElement('button');
    toggle.type = 'button';
    toggle.className = 'tile-toggle';
    toggle.setAttribute('aria-controls', body.id);
    function sync() {
      const open = expanded.has(job.id);
      body.hidden = !open;
      toggle.textContent = open ? '−' : '+';
      toggle.setAttribute('aria-expanded', String(open));
      toggle.setAttribute('aria-label', `${open ? 'Réduire' : 'Déplier'} ${job.display_name}`);
    }
    toggle.addEventListener('click', () => {
      if (expanded.has(job.id)) expanded.delete(job.id);
      else expanded.add(job.id);
      sync();
    });
    header.addEventListener('click', event => {
      if (!event.target.closest('button, a, input')) toggle.click();
    });
    header.append(toggle);
    sync();
    tile.append(header, body);
    return tile;
  }
  return {createFileTile, createJobTile};
});
