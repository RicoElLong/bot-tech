(() => {
  const quiz = document.getElementById('quiz');
  if (!quiz) return;

  const steps = quiz.querySelectorAll('.quiz-step');
  const fill = document.getElementById('quizProgress');
  const count = document.getElementById('quizCount');
  const btnBack = document.getElementById('quizBack');
  const btnNext = document.getElementById('quizNext');
  const nav = document.getElementById('quizNav');
  const errorBox = document.getElementById('quizError');
  const field = name => quiz.querySelector(`[name="${name}"]`);
  const controls = quiz.querySelectorAll('.quiz-form input, .quiz-form textarea, .quiz-form select, .quiz-opt');
  const preferredDate = field('preferredDate');
  const preferredTime = field('preferredTime');
  const consent = field('personalDataConsent');

  function samaraToday() {
    const parts = new Intl.DateTimeFormat('en-CA', {timeZone: 'Europe/Samara', year: 'numeric',
      month: '2-digit', day: '2-digit'}).formatToParts(new Date());
    const map = Object.fromEntries(parts.filter(p => p.type !== 'literal').map(p => [p.type,p.value]));
    return [map.year,map.month,map.day].join('-');
  }
  function dayPlus(iso, offset) {
    const day = new Date(iso + 'T00:00:00Z');
    day.setUTCDate(day.getUTCDate() + offset);
    return day.toISOString().slice(0,10);
  }
  function refreshPreferred() {
    const today = samaraToday();
    preferredDate.min = today;
    preferredDate.max = dayPlus(today, 6);
    if (preferredDate.value && (preferredDate.value < preferredDate.min || preferredDate.value > preferredDate.max))
      preferredDate.value = '';
    const parts = new Intl.DateTimeFormat('en-GB', {timeZone:'Europe/Samara',hour:'2-digit',
      minute:'2-digit',hour12:false}).formatToParts(new Date());
    const hm = Object.fromEntries(parts.filter(p=>p.type!=='literal').map(p=>[p.type,p.value]));
    const nowClock = hm.hour.padStart(2,'0') + ':' + hm.minute.padStart(2,'0');
    for(const option of preferredTime.options) {
      if (!option.value) continue;
      option.disabled = preferredDate.value === today && option.value <= nowClock;
    }
    if (preferredTime.selectedOptions.length && preferredTime.selectedOptions[0].disabled) preferredTime.value='';
  }
  refreshPreferred();
  const answers = {brand: '', problem: ''};
  let step = 1;
  let sending = false;
  let pending = null;

  function validPhone(value) {
    const digits = value.replace(/\D/g, '');
    return /^\+?[0-9 ()-]+$/.test(value) && digits.length >= 10 && digits.length <= 15;
  }

  function updateUI() {
    steps.forEach(item => item.classList.toggle('active', Number(item.dataset.step) === step));
    fill.style.width = `${Math.min(step, 3) / 3 * 100}%`;
    count.textContent = step <= 3 ? `Шаг ${step} из 3` : 'Готово';
    btnBack.disabled = sending || step === 1;
    btnBack.style.visibility = step === 4 ? 'hidden' : 'visible';
    nav.style.display = step === 4 ? 'none' : 'flex';
    quiz.setAttribute('aria-busy', String(sending));
    const ready = step === 1 ? Boolean(answers.brand) : step === 2 ? Boolean(answers.problem)
      : Boolean(field('quizName').value.trim()) && validPhone(field('quizPhone').value.trim())
        && Boolean(preferredDate.value) && Boolean(preferredTime.value)
        && preferredDate.value >= preferredDate.min && preferredDate.value <= preferredDate.max
        && !preferredTime.selectedOptions[0]?.disabled && consent.checked;
    btnNext.disabled = sending || !ready;
    btnNext.textContent = sending ? 'Отправляем…' : step === 3 ? 'Отправить заявку' : 'Далее →';
  }

  function showError(message = '') {
    errorBox.textContent = message;
    errorBox.hidden = !message;
  }

  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    return Array.from(bytes, (value, index) =>
      ([4, 6, 8, 10].includes(index) ? '-' : '') + value.toString(16).padStart(2, '0')).join('');
  }

  quiz.querySelectorAll('.quiz-grid').forEach(grid => {
    grid.querySelectorAll('.quiz-opt').forEach(option => {
      option.addEventListener('click', () => {
        if (sending) return;
        grid.querySelectorAll('.quiz-opt').forEach(item => {
          item.classList.toggle('selected', item === option);
          item.setAttribute('aria-pressed', String(item === option));
        });
        answers[grid.dataset.field] = option.dataset.value;
        showError();
        updateUI();
      });
    });
  });
  preferredDate.addEventListener('change', refreshPreferred);
  preferredTime.addEventListener('focus', refreshPreferred);
  controls.forEach(input => input.addEventListener('input', () => {
    showError();
    updateUI();
  }));

  btnNext.addEventListener('click', async () => {
    if (sending || step === 4) return;
    if (step < 3) {
      if ((step === 1 && !answers.brand) || (step === 2 && !answers.problem)) return;
      step++;
      updateUI();
      return;
    }
    refreshPreferred();
    if (!field('quizName').reportValidity() || !field('quizPhone').reportValidity()
        || !validPhone(field('quizPhone').value.trim()) || !preferredDate.reportValidity()
        || !preferredTime.reportValidity() || preferredTime.selectedOptions[0]?.disabled || !consent.reportValidity()) {
      showError('Проверьте дату, время и отдельное согласие на обработку данных.'); return;
    }
    const data = {...answers, name: field('quizName').value.trim(), phone: field('quizPhone').value.trim(),
      comment: field('quizComment').value.trim(), website: field('website').value,
      preferred_date: preferredDate.value, preferred_time: preferredTime.value,
      personal_data_consent: consent.checked, consent_version: 'pd-2026-11-09-v1'};
    const snapshot = JSON.stringify(data);
    if (!pending || pending.snapshot !== snapshot) pending = {snapshot, id: uuid()};
    sending = true;
    controls.forEach(input => { input.disabled = true; });
    showError();
    updateUI();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/api/requests', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        credentials: 'omit', signal: controller.signal,
        body: JSON.stringify({...data, submission_id: pending.id})
      });
      const result = await response.json();
      if (!response.ok || result.accepted !== true || !Number.isSafeInteger(result.request_id)
          || result.request_id <= 0) {
        throw new Error(typeof result.error === 'string' ? result.error : 'Не удалось подтвердить сохранение заявки.');
      }
      document.getElementById('sumBrand').textContent = data.brand;
      document.getElementById('sumProblem').textContent = data.problem;
      document.getElementById('sumRequest').textContent = `№${result.request_id}`;
      document.getElementById('sumPreferred').textContent = `${data.preferred_date.split('-').reverse().join('.')} ${data.preferred_time} (ожидает подтверждения)`;
      step = 4;
      pending = null;
    } catch (error) {
      const message = error.name === 'AbortError' || error instanceof TypeError || error instanceof SyntaxError
        ? 'Не удалось получить подтверждение. Проверьте интернет и повторите отправку — данные сохранены в форме.'
        : error.message;
      showError(message);
    } finally {
      clearTimeout(timer);
      sending = false;
      controls.forEach(input => { input.disabled = false; });
      updateUI();
    }
  });

  btnBack.addEventListener('click', () => {
    if (!sending && step > 1 && step < 4) {
      step--;
      showError();
      updateUI();
    }
  });
  updateUI();
})();
