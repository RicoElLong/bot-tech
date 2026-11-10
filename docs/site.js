'use strict';

const header = document.getElementById('header');
const washerDrum = document.querySelector('[data-washer-drum]');
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
let scrollQueued = false;
function updateHeroScroll() {
  const scrollY = window.scrollY;
  header.classList.toggle('is-scrolled', scrollY > 12);
  const angle = reducedMotion.matches ? 0 : (Math.max(0, scrollY) * 0.7) % 360;
  washerDrum.setAttribute('transform', 'rotate(' + angle + ' 244 283)');
  scrollQueued = false;
}
window.addEventListener('scroll', () => {
  if (!scrollQueued) {
    scrollQueued = true;
    window.requestAnimationFrame(updateHeroScroll);
  }
}, { passive: true });
reducedMotion.addEventListener('change', updateHeroScroll);
updateHeroScroll();

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
