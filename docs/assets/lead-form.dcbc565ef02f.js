'use strict';
(() => {
  const BRANDS = ['Bosch', 'LG', 'Samsung', 'Indesit', 'Ariston', 'Beko', 'Candy', 'Electrolux', 'Haier', 'Siemens', 'Whirlpool', 'Atlant', 'Zanussi', 'Другая / не знаю'];
  const PROBLEMS = ['Не сливает воду', 'Не набирает воду', 'Не отжимает', 'Не крутится барабан', 'Не включается', 'Не греет воду', 'Протекает', 'Шумит / вибрирует', 'Не открывается дверца', 'Показывает ошибку', 'Выбивает автомат', 'Другое / не знаю'];
  const dialog = document.getElementById('lead-dialog');
  if (!dialog) return;
  const form = dialog.querySelector('form');
  const heading = dialog.querySelector('#lead-title');
  const error = dialog.querySelector('#lead-error');
  const back = dialog.querySelector('[data-lead-back]');
  const submit = dialog.querySelector('[type=submit]');
  const status = dialog.querySelector('#lead-status');
  const fields = [...dialog.querySelectorAll('[data-lead-step]')];
  const state = { step: 1, brand: '', problem: '', sending: false, ready: false, version: '', submission: '', sent: false };
  let opener = null;
  let signature = '';
  let apiBase = '';
  try {
    const value = window.LEAD_FORM_CONFIG?.apiBase || '';
    if (value) {
      const parsed = new URL(value);
      if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.search || parsed.hash || !['', '/'].includes(parsed.pathname)) throw new Error();
      apiBase = parsed.origin;
    }
  } catch { apiBase = ''; }

  function showError(message) { error.textContent = message; error.hidden = !message; }
  function update(focus = true) {
    fields.forEach(field => { field.hidden = Number(field.dataset.leadStep) !== state.step; });
    heading.textContent = ['Какая марка стиральной машины?', 'Что случилось с машинкой?', 'Как с вами связаться?', 'Заявка получена'][state.step - 1];
    dialog.querySelector('#lead-progress').textContent = state.step < 4 ? `Шаг ${state.step} из 3` : 'Готово';
    dialog.querySelector('progress').value = Math.min(state.step, 3);
    back.hidden = state.step === 1 || state.step === 4;
    submit.hidden = state.step !== 3;
    submit.disabled = !state.ready || state.sending;
    submit.textContent = state.sending ? 'Отправляем…' : 'Отправить заявку';
    dialog.querySelector('.lead-actions').hidden = state.step === 4;
    dialog.querySelector('#lead-summary').textContent = `${state.brand} · ${state.problem}`;
    dialog.querySelector('.lead-close').disabled = state.sending;
    if (focus) heading.focus({ preventScroll: true });
    dialog.querySelector('.lead-body').scrollTop = 0;
  }
  function options(field, items) {
    const group = dialog.querySelector(`[data-options="${field}"]`);
    items.forEach(value => {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'lead-option'; button.textContent = value;
      button.setAttribute('aria-pressed', 'false');
      button.addEventListener('click', () => {
        selectOption(field, value);
        state.step = field === 'brand' && opener?.dataset.serviceProblem && state.problem ? 3 : state.step + 1;
        showError(''); update();
      });
      group.append(button);
    });
  }
  function selectOption(field, value) {
    state[field] = value;
    dialog.querySelectorAll(`[data-options="${field}"] button`).forEach(button => {
      button.setAttribute('aria-pressed', String(button.textContent === value));
    });
  }
  options('brand', BRANDS); options('problem', PROBLEMS);
  function reset() {
    form.reset(); state.step = 1; state.brand = ''; state.problem = ''; state.sent = false;
    state.submission = ''; signature = '';
    dialog.querySelectorAll('[aria-pressed]').forEach(b => b.setAttribute('aria-pressed', 'false'));
  }
  function open(event) {
    event.preventDefault(); opener = event.currentTarget;
    if (state.sent) reset();
    const problem = opener.dataset.serviceProblem;
    if (PROBLEMS.includes(problem)) {
      selectOption('problem', problem);
      state.step = state.brand ? 3 : 1;
    }
    if (!dialog.open) dialog.showModal();
    document.body.classList.add('lead-open');
    showError(''); update();
  }
  document.querySelectorAll('[data-lead-open]').forEach(button => button.addEventListener('click', open));
  function close() { if (!state.sending) dialog.close(); }
  dialog.querySelector('.lead-close').addEventListener('click', close);
  dialog.addEventListener('cancel', event => { if (state.sending) event.preventDefault(); });
  dialog.addEventListener('close', () => { document.body.classList.remove('lead-open'); opener?.focus(); });
  dialog.addEventListener('click', event => { if (event.target === dialog) close(); });
  back.addEventListener('click', () => { if (!state.sending) { state.step -= 1; showError(''); update(); } });
  dialog.querySelector('[data-lead-done]').addEventListener('click', close);

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (state.step !== 3 || !state.ready || state.sending) return;
    showError('');
    const name = form.elements.name.value.trim();
    const phone = form.elements.phone.value.trim();
    const comment = form.elements.comment.value.trim();
    const consent = form.elements.consent.checked;
    if (!name) { showError('Укажите, как к вам обращаться.'); form.elements.name.focus(); return; }
    if (!/^\+?[0-9 ()\-]+$/.test(phone)) { showError('Введите телефон, например +7 999 123-45-67.'); form.elements.phone.focus(); return; }
    let digits = phone.replace(/\D/g, '');
    if (digits.length === 10) digits = '7' + digits;
    if (digits.length === 11 && digits[0] === '8') digits = '7' + digits.slice(1);
    if (digits.length !== 11 || digits[0] !== '7') { showError('Проверьте телефон: нужно 11 цифр, начиная с 7 или 8.'); form.elements.phone.focus(); return; }
    if (!consent) { showError('Для обратной связи необходимо ваше согласие на обработку данных.'); form.elements.consent.focus(); return; }
    const payload = { brand: state.brand, problem: state.problem, name, phone: '+' + digits, comment, consent: true, consent_version: state.version, website: form.elements.website.value };
    const nextSignature = JSON.stringify(payload);
    // Повтор после сетевой ошибки получает тот же ID, а не создаёт дубль.
    if (!state.submission || signature !== nextSignature) {
      state.submission = crypto.randomUUID(); signature = nextSignature;
    }
    payload.submission_id = state.submission;
    state.sending = true;
    form.querySelectorAll('input,textarea').forEach(input => { input.disabled = true; });
    back.disabled = true; update(false);
    try {
      const response = await fetch(apiBase + '/api/leads', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
        credentials: 'omit', referrerPolicy: 'no-referrer', signal: AbortSignal.timeout(15000),
      });
      const result = await response.json();
      if (!response.ok || result.accepted !== true || typeof result.request_id !== 'string') {
        const fallback = response.status === 429 ? 'Слишком много попыток. Повторите через 10 минут.' : 'Сервер не принял заявку. Попробуйте ещё раз.';
        throw new Error(typeof result.detail === 'string' ? result.detail : fallback);
      }
      state.step = 4; state.sent = true;
      dialog.querySelector('#lead-reference').textContent = result.request_id;
      form.reset(); // Не сохраняем контакты в localStorage, URL, аналитике или логах.
    } catch (failure) {
      showError(failure.name === 'TimeoutError' || failure.name === 'TypeError'
        ? 'Не удалось подтвердить отправку. Данные сохранены в форме — попробуйте ещё раз.'
        : failure.message || 'Не удалось отправить заявку. Попробуйте ещё раз.');
    } finally {
      state.sending = false;
      form.querySelectorAll('input,textarea').forEach(input => { input.disabled = false; });
      back.disabled = false; update(false);
      if (state.sent) heading.focus();
      else if (!error.hidden) error.scrollIntoView({ block: 'nearest' });
    }
  });

  async function configure() {
    if (!apiBase) { status.textContent = 'Приём заявок с сайта пока настраивается. Форма доступна для ознакомления, отправка ещё не подключена.'; update(false); return; }
    try {
      const response = await fetch(apiBase + '/api/config', { credentials: 'omit', referrerPolicy: 'no-referrer', signal: AbortSignal.timeout(8000) });
      if (!response.ok) throw new Error();
      const config = await response.json();
      state.ready = config.ready === true && Boolean(config.operator?.name && config.operator?.address && config.operator?.email && config.consent_version);
      state.version = config.consent_version || '';
      dialog.querySelector('#lead-operator').textContent = state.ready ? `Оператор: ${config.operator.name}.` : '';
      status.textContent = state.ready ? 'Мастер свяжется с вами в ближайшее рабочее время. Telegram для отправки заявки не нужен.' : 'Приём заявок пока настраивается. Отправка временно недоступна.';
    } catch { status.textContent = 'Сервис обратной связи временно недоступен. Пожалуйста, попробуйте позже.'; }
    update(false);
  }
  configure();
})();
