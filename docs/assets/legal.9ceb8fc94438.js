'use strict';
(async () => {
  let base = '';
  try {
    const url = new URL(window.LEAD_FORM_CONFIG?.apiBase || '');
    if (url.protocol !== 'https:' || url.username || url.password) return;
    base = url.origin;
    const response = await fetch(base + '/api/config', { credentials: 'omit', referrerPolicy: 'no-referrer', signal: AbortSignal.timeout(8000) });
    if (!response.ok) return;
    const config = await response.json();
    if (!config.ready || !config.operator?.name || !config.operator?.address || !config.operator?.email) return;
    document.querySelectorAll('[data-operator]').forEach(element => {
      element.textContent = config.operator[element.dataset.operator] || '';
    });
    document.querySelector('.legal-pending').hidden = true;
  } catch { /* Документ явно остаётся шаблоном; приём данных не включается. */ }
})();
