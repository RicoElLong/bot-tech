'use strict';

let pending = null;

document.addEventListener('submit', event => {
  const form = event.target;
  const button = event.submitter;
  if (!button || form.method !== 'post') return;
  if (pending) {
    event.preventDefault();
    return;
  }

  pending = { form, button, label: button.textContent };
  form.setAttribute('aria-busy', 'true');
  button.setAttribute('aria-disabled', 'true');
  button.textContent = 'Подождите…';
});

window.addEventListener('pageshow', () => {
  if (!pending) return;
  const { form, button, label } = pending;
  form.removeAttribute('aria-busy');
  button.removeAttribute('aria-disabled');
  button.textContent = label;
  pending = null;
});
