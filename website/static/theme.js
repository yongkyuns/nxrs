/* Set the theme before first paint. Storage is optional, including in embeds. */
(() => {
  document.documentElement.classList.add('js');
  let preference;
  try { preference = localStorage.getItem('nxrs-theme'); } catch { /* Private browsing. */ }
  document.documentElement.dataset.theme = preference === 'light' || preference === 'dark'
    ? preference : matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
})();
