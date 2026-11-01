'use strict';

const header = document.getElementById('header');
let scrollQueued = false;
function updateHeader() {
  header.classList.toggle('is-scrolled', window.scrollY > 12);
  scrollQueued = false;
}
window.addEventListener('scroll', () => {
  if (!scrollQueued) {
    scrollQueued = true;
    window.requestAnimationFrame(updateHeader);
  }
}, { passive: true });
updateHeader();

const toggle = document.querySelector('.menu-toggle');
const menu = document.getElementById('mobile-nav');
toggle.hidden = false;
function closeMenu() {
  toggle.setAttribute('aria-expanded', 'false');
  toggle.setAttribute('aria-label', 'Открыть меню');
  menu.hidden = true;
}
toggle.addEventListener('click', () => {
  const open = toggle.getAttribute('aria-expanded') !== 'true';
  toggle.setAttribute('aria-expanded', String(open));
  toggle.setAttribute('aria-label', open ? 'Закрыть меню' : 'Открыть меню');
  menu.hidden = !open;
});
menu.addEventListener('click', (event) => {
  if (event.target.closest('a')) closeMenu();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !menu.hidden) {
    closeMenu();
    toggle.focus();
  }
});
window.matchMedia('(min-width: 901px)').addEventListener('change', (event) => {
  if (event.matches) closeMenu();
});

document.getElementById('year').textContent = new Date().getFullYear();
