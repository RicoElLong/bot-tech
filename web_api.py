"""RU-hosted callback API. Telegram receives only a request ID and a protected link."""
import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlparse
from uuid import UUID

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from web_leads import BRANDS, PROBLEMS, CONSENT_VERSION, LeadStore, client_hash

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env.local')
load_dotenv(ROOT / '.env')
logging.getLogger('httpx').setLevel(logging.WARNING)


@dataclass
class Settings:
    enabled: bool
    origin: str
    public_url: str
    operator_name: str
    operator_address: str
    operator_email: str
    database: str
    token: str
    admin_ids: tuple
    admin_user: str
    admin_password: str
    location: str

    @classmethod
    def from_env(cls):
        return cls(os.getenv('WEB_FORM_ENABLED') == 'true',
                   os.getenv('WEB_SITE_ORIGIN', 'https://ricoellong.github.io'),
                   os.getenv('WEB_API_PUBLIC_URL', '').rstrip('/'),
                   os.getenv('WEB_OPERATOR_NAME', ''), os.getenv('WEB_OPERATOR_ADDRESS', ''),
                   os.getenv('WEB_OPERATOR_EMAIL', ''), os.getenv('WEB_DATABASE_PATH', str(ROOT / 'data/web-leads.sqlite3')),
                   os.getenv('BOT_TOKEN', ''), tuple(v.strip() for v in os.getenv('ADMIN_IDS', '').split(',') if v.strip()),
                   os.getenv('WEB_ADMIN_USER', 'admin'), os.getenv('WEB_ADMIN_PASSWORD', ''),
                   os.getenv('WEB_DATA_LOCATION_CONFIRMED', ''))

    @property
    def ready(self):
        public = urlparse(self.public_url)
        return bool(self.enabled and self.location == 'ru' and self.token and self.admin_ids
                    and all(re.fullmatch(r'\d+', value) for value in self.admin_ids)
                    and public.scheme == 'https' and public.netloc and not public.username
                    and self.operator_name and self.operator_address
                    and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', self.operator_email)
                    and len(self.admin_password) >= 20 and self.admin_user)


def clean_text(value, minimum, maximum):
    if not isinstance(value, str):
        raise ValueError('Проверьте поля формы.')
    value = value.strip()
    if not minimum <= len(value) <= maximum or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise ValueError('Проверьте длину и содержимое полей.')
    return value


def validate(data):
    if not isinstance(data, dict):
        raise ValueError('Некорректная форма.')
    if data.get('consent') is not True or data.get('consent_version') != CONSENT_VERSION:
        raise ValueError('Для обратной связи необходимо отдельное согласие на обработку данных.')
    if data.get('website'):
        raise ValueError('Не удалось проверить форму.')
    if data.get('brand') not in BRANDS or data.get('problem') not in PROBLEMS:
        raise ValueError('Выберите марку и неисправность из списка.')
    phone = clean_text(data.get('phone'), 10, 24)
    if not re.fullmatch(r'\+?[0-9 ()\-]+', phone):
        raise ValueError('Введите российский телефон в формате +7 999 123-45-67.')
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 10:
        digits = '7' + digits
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    if len(digits) != 11 or not digits.startswith('7'):
        raise ValueError('Введите российский телефон в формате +7 999 123-45-67.')
    submission = str(UUID(data.get('submission_id', '')))
    return submission, {'name': clean_text(data.get('name'), 1, 80), 'phone': '+' + digits,
                        'brand': data['brand'], 'problem': data['problem'],
                        'comment': clean_text(data.get('comment', ''), 0, 1000)}


async def deliver_notifications(settings, store):
    if not settings.ready:
        return
    for item in store.pending():
        if not store.get(item['lead_id']):
            store.mark_attempt(item['id'], True)
            continue
        # Имя, номер телефона и свободный комментарий не передаются Telegram.
        text = ('Новая или обновлённая заявка с сайта БытТех.\n'
                f'Номер: {item["lead_id"]}\n'
                f'Карточка: {settings.public_url}/admin/leads/{item["lead_id"]}')
        success = True
        async with httpx.AsyncClient(timeout=12) as client:
            for uid in settings.admin_ids:
                if store.delivered_to(item['id'], uid):
                    continue
                try:
                    response = await client.post(f'https://api.telegram.org/bot{settings.token}/sendMessage',
                                                 json={'chat_id': uid, 'text': text, 'link_preview_options': {'is_disabled': True}})
                    delivered = response.is_success and response.json().get('ok') is True
                    if delivered:
                        store.mark_delivered(item['id'], uid)
                    success = delivered and success
                except (httpx.HTTPError, ValueError):
                    success = False
        store.mark_attempt(item['id'], success)


def create_app(settings=None, notifications=True):
    settings = settings or Settings.from_env()
    store = LeadStore(settings.database)
    salt = secrets.token_bytes(32)

    @asynccontextmanager
    async def lifespan(app):
        async def worker():
            while True:
                try:
                    store.purge(30)
                    await deliver_notifications(settings, store)
                except Exception:
                    logging.getLogger(__name__).warning('Ошибка очереди заявок; повтор через 30 секунд.')
                await asyncio.sleep(30)
        if not notifications:
            yield
            return
        task = asyncio.create_task(worker())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.add_middleware(CORSMiddleware, allow_origins=[settings.origin], allow_methods=['GET', 'POST'],
                       allow_headers=['Content-Type'], allow_credentials=False)

    @app.middleware('http')
    async def security(request, call_next):
        if request.method == 'POST':
            if request.headers.get('origin') != settings.origin:
                return JSONResponse({'detail': 'Недопустимый источник запроса.'}, status_code=403)
            if request.headers.get('content-type', '').split(';')[0] != 'application/json':
                return JSONResponse({'detail': 'Ожидается JSON.'}, status_code=415)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"
        return response

    @app.get('/api/config')
    def config():
        return {'ready': settings.ready, 'operator': {'name': settings.operator_name,
                'address': settings.operator_address, 'email': settings.operator_email},
                'consent_version': CONSENT_VERSION, 'retention_days': 30}

    @app.post('/api/leads')
    async def leads(request: Request):
        if not settings.ready:
            raise HTTPException(503, 'Приём заявок пока не подключён. Попробуйте позже.')
        client = request.client.host if request.client else 'unknown'
        if not store.consume_rate(client_hash(client, salt), int(time.time())):
            raise HTTPException(429, 'Слишком много попыток. Повторите через 10 минут.')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16_384:
                raise HTTPException(413, 'Слишком большой запрос.')
        try:
            data = json.loads(body)
            submission, payload = validate(data)
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            lead_id = store.save(submission, payload, digest, CONSENT_VERSION)
        except (ValueError, TypeError, AttributeError) as error:
            raise HTTPException(422, str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else 'Проверьте поля формы.') from None
        return {'accepted': True, 'request_id': lead_id,
                'message': 'Заявка получена. Мастер свяжется с вами в ближайшее рабочее время.'}

    basic = HTTPBasic()

    def admin_auth(credentials: HTTPBasicCredentials = Depends(basic)):
        user_ok = secrets.compare_digest(credentials.username.encode(), settings.admin_user.encode())
        password_ok = secrets.compare_digest(credentials.password.encode(), settings.admin_password.encode())
        if not settings.ready or not user_ok or not password_ok:
            raise HTTPException(401, 'Доступ запрещён.', headers={'WWW-Authenticate': 'Basic'})

    @app.get('/admin/leads/{lead_id}', response_class=HTMLResponse, dependencies=[Depends(admin_auth)])
    def card(lead_id: str):
        lead = store.get(lead_id)
        if not lead:
            raise HTTPException(404, 'Заявка не найдена.')
        fields = [('Имя',lead['name']), ('Телефон',lead['phone']), ('Марка',lead['brand']),
                  ('Поломка',lead['problem']), ('Комментарий',lead['comment'] or '—'),
                  ('Обновлено (UTC)',lead['updated_at']),('Согласие (UTC)',lead['consent_at']),
                  ('Версия согласия',lead['consent_version'])]
        rows = ''.join(f'<dt>{name}</dt><dd>{html.escape(value)}</dd>' for name,value in fields)
        return ('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<meta name="robots" content="noindex,nofollow"><title>Заявка БытТех</title>'
                '<style>body{font:17px/1.6 system-ui;max-width:700px;margin:30px auto;padding:20px}'
                'dt{font-weight:700;margin-top:15px}dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}</style>'
                f'<h1>Заявка БытТех</h1><dl>{rows}</dl></html>')

    return app


app = create_app()
