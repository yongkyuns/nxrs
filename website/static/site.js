const root = document.documentElement;
const base = document.body.dataset.base;
const themeButton = document.querySelector('#theme-toggle');
const media = matchMedia('(prefers-color-scheme: dark)');
function updateThemeButton() {
  const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
  themeButton.textContent = next === 'dark' ? 'Dark' : 'Light';
  themeButton.setAttribute('aria-label', `Switch to ${next} theme`);
}
themeButton.addEventListener('click', () => {
  root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem('nxrs-theme', root.dataset.theme); } catch { /* Storage is optional. */ }
  updateThemeButton();
});
media.addEventListener('change', () => {
  let saved;
  try { saved = localStorage.getItem('nxrs-theme'); } catch { /* Storage is optional. */ }
  if (!saved) { root.dataset.theme = media.matches ? 'dark' : 'light'; updateThemeButton(); }
});
updateThemeButton();

const menu = document.querySelector('#menu-toggle');
const sidebar = document.querySelector('#sidebar');
menu?.addEventListener('click', () => {
  const open = menu.getAttribute('aria-expanded') !== 'true';
  menu.setAttribute('aria-expanded', String(open));
  sidebar.dataset.open = String(open);
});

for (const block of document.querySelectorAll('.prose pre')) {
  const code = block.querySelector('code');
  if (!code) continue;
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'copy-code'; button.textContent = 'Copy code';
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(code.textContent);
      button.textContent = 'Copied';
    } catch { button.textContent = 'Select and copy the code'; }
    setTimeout(() => { button.textContent = 'Copy code'; }, 1800);
  });
  block.append(button);
}

const dialog = document.querySelector('#search-dialog');
const searchButton = document.querySelector('.search-open');
const input = document.querySelector('#search-input');
const results = document.querySelector('#search-results');
const status = document.querySelector('#search-status');
let searchIndex, timer, generation = 0;
const normalize = value => value.normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase();
function openSearch() { if (!dialog.open) dialog.showModal(); input.focus(); }
searchButton.addEventListener('click', openSearch);
document.querySelector('#search-close').addEventListener('click', () => dialog.close());
dialog.addEventListener('close', () => { searchButton.focus(); });
document.addEventListener('keydown', event => {
  // A search input can consume Escape to clear its value before the browser
  // cancels the dialog. Close the modal on the first press, with or without a
  // query, and leave focus restoration to the dialog's close handler.
  if (event.key === 'Escape' && dialog.open) {
    event.preventDefault(); dialog.close(); return;
  }
  const editing = event.target.matches('input,textarea,select') || event.target.isContentEditable;
  if ((!editing && event.key === '/') || ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k')) {
    event.preventDefault(); openSearch();
  }
  if (event.key === 'Escape' && menu?.getAttribute('aria-expanded') === 'true') {
    menu.setAttribute('aria-expanded', 'false'); sidebar.dataset.open = 'false'; menu.focus();
  }
});
async function search() {
  const run = ++generation;
  const query = normalize(input.value.trim()).slice(0, 180);
  results.replaceChildren();
  if (query.length < 2) { status.textContent = 'Enter at least two characters.'; return; }
  status.textContent = 'Searching…';
  try {
    if (!searchIndex) {
      const response = await fetch(`${base}search.json`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const entries = await response.json();
      if (!Array.isArray(entries)) throw new Error('Invalid search index');
      searchIndex = entries.filter(item => typeof item.url === 'string' && item.url.startsWith(base))
        .map(item => ({ ...item, normalizedTitle: normalize(item.title), normalizedHeadings: normalize(item.headings), normalizedText: normalize(item.text) }));
    }
    if (run !== generation) return;
    const words = query.split(/\s+/).filter(Boolean);
    const matches = searchIndex.map(item => {
      const haystack = `${item.normalizedTitle} ${item.normalizedHeadings} ${item.normalizedText}`;
      if (!words.every(word => haystack.includes(word))) return null;
      let score = item.section === 'User guide' ? 2 : 0;
      for (const word of words) score += (item.normalizedTitle.includes(word) ? 12 : 0) + (item.normalizedHeadings.includes(word) ? 5 : 0);
      if (item.normalizedTitle.includes(query)) score += 25;
      return { item, score };
    }).filter(Boolean).sort((a, b) => b.score - a.score || a.item.title.localeCompare(b.item.title));
    for (const { item } of matches.slice(0, 12)) {
      const li = document.createElement('li'), link = document.createElement('a');
      const heading = document.createElement('strong'), section = document.createElement('small'), excerpt = document.createElement('p');
      link.href = item.url; heading.textContent = item.title; section.textContent = item.section;
      const at = Math.max(0, item.normalizedText.indexOf(words[0]) - 55);
      excerpt.textContent = `${at ? '…' : ''}${item.text.slice(at, at + 210)}${item.text.length > at + 210 ? '…' : ''}`;
      link.append(heading, section, excerpt); li.append(link); results.append(li);
    }
    status.textContent = matches.length ? `${matches.length} matching pages${matches.length > 12 ? '; showing the first 12' : ''}.` : 'No matching pages. Try fewer words or browse the reference library.';
  } catch {
    if (run === generation) status.textContent = 'Search could not load. Try again, or use the guide and reference navigation.';
  }
}
input.addEventListener('input', () => { clearTimeout(timer); generation++; timer = setTimeout(search, 120); });

const load = document.querySelector('#load-noon');
if (load) {
  const unload = document.querySelector('#unload-noon');
  const holder = document.querySelector('#noon-frame');
  const noonStatus = document.querySelector('#noon-status');
  const direct = document.querySelector('#noon-direct');
  const placeholder = holder.firstElementChild.cloneNode(true);
  let timeout;
  load.addEventListener('click', () => {
    if (holder.querySelector('iframe')) return;
    const url = new URL(direct.href);
    if (url.origin !== 'https://yongkyuns.github.io' || url.pathname !== '/noon/') {
      noonStatus.textContent = 'The configured demo URL is not supported. Use the direct link.'; return;
    }
    const frame = document.createElement('iframe');
    frame.title = 'Noon: Move the whole diagram — interactive animation playground';
    frame.referrerPolicy = 'no-referrer'; frame.allowFullscreen = true;
    frame.allow = 'fullscreen';
    frame.addEventListener('load', () => {
      clearTimeout(timeout);
      noonStatus.textContent = 'The embedded page loaded. Engine readiness and playback controls are shown inside Noon.';
    });
    frame.addEventListener('error', () => {
      clearTimeout(timeout); noonStatus.textContent = 'The player could not load. Open the scene directly using the link below.';
    });
    noonStatus.textContent = 'Loading Noon. This optional player downloads its own WebAssembly and Python runtime.';
    load.hidden = true; unload.hidden = false;
    timeout = setTimeout(() => { noonStatus.textContent = 'The player is taking longer to load. You can unload it or open the same scene directly.'; }, 45000);
    frame.src = url.href; holder.replaceChildren(frame); unload.focus();
  });
  unload.addEventListener('click', () => {
    clearTimeout(timeout); holder.replaceChildren(placeholder.cloneNode(true));
    load.hidden = false; unload.hidden = true;
    noonStatus.textContent = 'Demo unloaded. The documentation remains available without its runtime.';
    load.focus();
  });
}
