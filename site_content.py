"""Editable text in explicitly marked locations of the public site template."""

import json
import re
from html import escape, unescape

from admin_store import AdminConflict, clean_text
from storage import now


HOURS = re.compile(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]')
MARKER = re.compile(r'<!-- site:([a-z0-9-]+) -->(.*?)<!-- /site:\1 -->', re.S)
PRICE = re.compile(r'от ([0-9\s]+) ₽')
SERVICES = {
    'drum': 'Не вращается барабан', 'heat': 'Не нагревает воду',
    'leak': 'Протекает стиральная машина', 'drain': 'Не сливает воду',
    'spin': 'Не отжимает', 'noise': 'Сильно шумит',
    'power': 'Не включается', 'fill': 'Не набирает воду',
}


def defaults(source):
    values = {key: unescape(value) for key, value in MARKER.findall(source)}
    if not values:
        return None
    opening, closing = values['hours'].split('–')
    services = {}
    for key in SERVICES:
        price = PRICE.fullmatch(values[f'{key}-price'])
        services[key] = {
            'description': values[f'{key}-description'],
            'price': int(re.sub(r'\s', '', price[1])),
        }
    return {'opening': opening, 'closing': closing, 'services': services}


def from_form(form):
    opening, closing = form.get('opening', ''), form.get('closing', '')
    if not (HOURS.fullmatch(opening) and HOURS.fullmatch(closing) and opening < closing):
        raise ValueError('Укажите время начала и окончания работы в пределах одного дня.')
    services = {}
    for key in SERVICES:
        amount = form.get(f'{key}-price', '')
        if not re.fullmatch(r'[1-9][0-9]{0,5}', amount):
            raise ValueError('Укажите стартовые цены от 1 до 999 999 рублей.')
        services[key] = {
            'description': clean_text(form.get(f'{key}-description', ''), 160, required=True),
            'price': int(amount),
        }
    return {'opening': opening, 'closing': closing, 'services': services}


def render(source, content):
    values = {'hours': f'{content["opening"]}–{content["closing"]}'}
    for key, item in content['services'].items():
        price = f'{item["price"]:,}'.replace(',', '\u00a0')
        values[f'{key}-price'] = f'от {price} ₽'
        values[f'{key}-description'] = item['description']
    return MARKER.sub(lambda m: escape(values.get(m[1], unescape(m[2]))), source)


def published(db):
    if db is None or not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_site_content'"
    ).fetchone():
        return None
    row = db.execute('SELECT published FROM admin_site_content WHERE id=1').fetchone()
    return json.loads(row['published']) if row and row['published'] else None


class SiteEditor:
    def __init__(self, admin, index_path):
        self.admin, self.index_path = admin, index_path

    def read(self):
        initial = defaults(self.index_path.read_text(encoding='utf-8'))
        row = self.admin.db.execute('SELECT * FROM admin_site_content WHERE id=1').fetchone()
        if initial is None:
            return None, None, 0, None
        if row is None:
            return initial, None, 0, None
        public = json.loads(row['published']) if row['published'] else None
        draft = json.loads(row['draft']) if row['draft'] else (public or initial)
        return draft, public, row['revision'], row['published_at']

    def save(self, data, revision, actor):
        with self.admin.transaction():
            current, _, current_revision, _ = self.read()
            if current is None:
                raise AdminConflict('Подключите актуальный шаблон сайта через WEB_SITE_ROOT.')
            if revision != current_revision:
                raise AdminConflict('Черновик уже изменился. Обновите страницу.')
            self.admin.db.execute('''INSERT INTO admin_site_content(id,draft,revision) VALUES(1,?,1)
                ON CONFLICT(id) DO UPDATE SET draft=excluded.draft,revision=revision+1''',
                (json.dumps(data, ensure_ascii=False),))
            self.admin.event(actor, 'Сохранён черновик сайта')

    def publish(self, revision, actor):
        with self.admin.transaction():
            draft, public, current_revision, _ = self.read()
            if draft is None or revision != current_revision:
                raise AdminConflict('Черновик изменился. Откройте его заново.')
            if not current_revision or draft == public:
                return
            self.admin.db.execute('''UPDATE admin_site_content
                SET published=draft,published_at=?,revision=revision+1 WHERE id=1''', (now(),))
            self.admin.event(actor, 'Опубликованы настройки сайта')
