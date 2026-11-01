"""Website intake and durable Telegram notifications, in the bot's event loop."""

import asyncio
from collections import OrderedDict, deque
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
from uuid import UUID

from aiohttp import web
from telegram import LinkPreviewOptions
from telegram.error import RetryAfter, TelegramError

from storage import SubmissionConflict, canonical_phone

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebSettings:
    host: str = '127.0.0.1'
    port: int = 8080
    origins: tuple = ('http://127.0.0.1:8080', 'http://localhost:8080')
    trusted_proxies: tuple = ('127.0.0.1/32', '::1/128')

    @classmethod
    def from_env(cls):
        port = int(os.getenv('WEB_PORT', '8080'))
        if not 1 <= port <= 65535:
            raise ValueError('WEB_PORT должен быть от 1 до 65535.')
        origins = tuple(value.strip().rstrip('/') for value in os.getenv(
            'WEB_ALLOWED_ORIGINS', ','.join(cls.origins)).split(',') if value.strip())
        if not origins:
            raise ValueError('Укажите WEB_ALLOWED_ORIGINS.')
        for origin in origins:
            parsed = urlsplit(origin)
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                    or parsed.path or parsed.query or parsed.fragment or parsed.username
                    or parsed.password):
                raise ValueError('WEB_ALLOWED_ORIGINS содержит недопустимый адрес.')
        proxies = tuple(value.strip() for value in os.getenv(
            'WEB_TRUSTED_PROXIES', ','.join(cls.trusted_proxies)).split(',') if value.strip())
        for proxy in proxies:
            ipaddress.ip_network(proxy)
        return cls(os.getenv('WEB_HOST', cls.host), port, origins, proxies)


class RateLimiter:
    def __init__(self, limit=5, window=600, capacity=4096):
        self.limit, self.window, self.capacity = limit, window, capacity
        self.entries = OrderedDict()

    def allow(self, key):
        timestamp = time.monotonic()
        entries = self.entries.pop(key, deque())
        while entries and entries[0] <= timestamp - self.window:
            entries.popleft()
        allowed = len(entries) < self.limit
        if allowed:
            entries.append(timestamp)
        self.entries[key] = entries
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)
        return allowed


def validated_submission(payload):
    if not isinstance(payload, dict):
        raise ValueError('Ожидается объект JSON.')

    def text(field, maximum, required=True, multiline=False):
        value = payload.get(field, '')
        if not isinstance(value, str):
            raise ValueError('Поля заявки должны содержать текст.')
        value = value.strip()
        controls = r'[\x00-\x08\x0b-\x1f\x7f]' if multiline else r'[\x00-\x1f\x7f]'
        if len(value) > maximum or (required and not value) or re.search(controls, value):
            raise ValueError('Проверьте заполнение и длину полей заявки.')
        return value

    submission_id = text('submission_id', 36)
    try:
        parsed = UUID(submission_id)
        if parsed.version != 4 or str(parsed) != submission_id:
            raise ValueError
    except ValueError:
        raise ValueError('Некорректный номер отправки. Обновите страницу.') from None
    if text('website', 200, required=False):
        raise ValueError('Не удалось принять форму. Обновите страницу.')
    phone = text('phone', 40)
    if not re.fullmatch(r'\+?[0-9 ()-]+', phone) or not 10 <= len(re.sub(r'\D', '', phone)) <= 15:
        raise ValueError('Введите корректный телефон: от 10 до 15 цифр.')
    data = {
        'name': text('name', 100),
        'phone': canonical_phone(phone),
        'brand': text('brand', 100),
        'problem': text('problem', 500),
        'comment': text('comment', 2000, required=False, multiline=True),
    }
    # Keep both notifications and the /admin card below Telegram's text limit,
    # including supplementary Unicode characters that occupy two UTF-16 units.
    if sum(len(value.encode('utf-16-le')) // 2 for value in data.values()) > 3400:
        raise ValueError('Сократите описание и комментарий.')
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return submission_id, digest, data


def website_notification(row):
    return (f'Новая заявка с сайта Быттех №{row["id"]}\n'
            f'Источник: сайт\nИмя: {row["contact_name"]}\nТелефон: {row["phone"]}\n'
            f'Марка: {row["brand"]}\nПоломка: {row["problem"]}\n'
            f'Комментарий: {row["comment"] or "—"}\n\nВсе заявки: /admin')


async def deliver_notifications(db, bot, admins):
    for row in db.due_web_notifications():
        if row['admin_id'] not in admins:
            db.finish_web_notification(row['id'], row['admin_id'], 'cancelled')
            continue
        try:
            await bot.send_message(chat_id=row['admin_id'], text=website_notification(row),
                                   parse_mode=None, link_preview_options=LinkPreviewOptions(is_disabled=True))
        except TelegramError as error:
            delay = min(300, 5 * 2 ** min(row['attempts'], 6))
            if isinstance(error, RetryAfter):
                retry_after = error.retry_after
                delay = max(delay, retry_after.total_seconds() if hasattr(retry_after, 'total_seconds') else retry_after)
            db.retry_web_notification(row['id'], row['admin_id'], delay)
            # Do not log exception text: SDK errors can contain credentials or user data.
            LOG.warning('Website notification %s failed: %s', row['id'], type(error).__name__)
        else:
            db.finish_web_notification(row['id'], row['admin_id'])


class WebsiteService:
    def __init__(self, db, bot, admins, settings=None, web_root=None):
        if not admins:
            raise ValueError('Для приёма заявок с сайта заполните ADMIN_IDS.')
        self.db, self.bot, self.admins = db, bot, frozenset(admins)
        self.settings = settings or WebSettings.from_env()
        self.web_root = Path(web_root) if web_root else ROOT / 'web'
        self.proxies = tuple(ipaddress.ip_network(item) for item in self.settings.trusted_proxies)
        self.limiter = RateLimiter()
        self.runner = None
        self.task = None

    def client_ip(self, request):
        peer = ipaddress.ip_address(request.remote or '0.0.0.0')
        if any(peer in network for network in self.proxies):
            forwarded = request.headers.get('X-Real-IP')
            if forwarded:
                try:
                    return str(ipaddress.ip_address(forwarded))
                except ValueError:
                    pass
        return str(peer)

    def make_app(self):
        @web.middleware
        async def responses(request, handler):
            origin = request.headers.get('Origin')
            is_intake = request.path == '/api/requests'
            if is_intake and origin not in self.settings.origins:
                response = web.json_response({'error': 'Этот адрес сайта не разрешён.'}, status=403)
            else:
                try:
                    response = await handler(request)
                except web.HTTPException as error:
                    response = web.json_response({'error': 'Запрос не может быть обработан.'}, status=error.status)
                except sqlite3.Error:
                    LOG.error('Website storage temporarily unavailable')
                    response = web.json_response({'error': 'Не удалось сохранить заявку. Повторите отправку.'}, status=503)
            if origin in self.settings.origins and is_intake:
                response.headers['Access-Control-Allow-Origin'] = origin
                response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
                response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
                response.headers['Vary'] = 'Origin'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['Referrer-Policy'] = 'same-origin'
            if request.path.startswith('/api/'):
                response.headers['Cache-Control'] = 'no-store'
            return response

        app = web.Application(client_max_size=12 * 1024, middlewares=[responses])
        app.router.add_post('/api/requests', self.intake)
        app.router.add_options('/api/requests', self.preflight)
        app.router.add_get('/api/health', self.health)
        app.router.add_get('/', self.index)
        app.router.add_get('/index.html', self.index)
        app.router.add_get('/request-form.js', self.form_script)
        return app

    async def intake(self, request):
        if request.content_type != 'application/json':
            return web.json_response({'error': 'Ожидается JSON.'}, status=415)
        try:
            payload = await asyncio.wait_for(request.json(), timeout=10)
            submission_id, digest, data = validated_submission(payload)
            existing = self.db.web_submission(submission_id, digest)
            if existing is not None:
                return web.json_response({'accepted': True, 'request_id': existing}, status=200)
        except SubmissionConflict as error:
            return web.json_response({'error': str(error)}, status=409)
        except (ValueError, UnicodeError, RecursionError):
            return web.json_response({'error': 'Проверьте имя, телефон и остальные поля заявки.'}, status=400)
        except asyncio.TimeoutError:
            return web.json_response({'error': 'Время отправки истекло. Попробуйте ещё раз.'}, status=408)
        if not self.limiter.allow(self.client_ip(request)):
            return web.json_response({'error': 'Слишком много заявок. Повторите через 10 минут.'},
                                      status=429, headers={'Retry-After': '600'})
        request_id, created = self.db.submit_website(submission_id, digest, data, self.admins)
        return web.json_response({'accepted': True, 'request_id': request_id}, status=201 if created else 200)

    async def health(self, request):
        self.db.db.execute('SELECT 1').fetchone()
        return web.json_response({'ok': True})

    async def preflight(self, request):
        return web.Response(status=204)

    async def index(self, request):
        return web.FileResponse(self.web_root / 'index.html')

    async def form_script(self, request):
        return web.FileResponse(self.web_root / 'request-form.js')

    async def notification_loop(self):
        while True:
            try:
                await deliver_notifications(self.db, self.bot, self.admins)
            except Exception as error:
                LOG.error('Website notification worker failed: %s', type(error).__name__)
            await asyncio.sleep(2)

    async def start(self):
        self.runner = web.AppRunner(self.make_app(), access_log=None)
        try:
            await self.runner.setup()
            await web.TCPSite(self.runner, self.settings.host, self.settings.port).start()
        except BaseException:
            await self.runner.cleanup()
            raise
        self.task = asyncio.create_task(self.notification_loop(), name='website-notifications')

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task


async def start_website(application):
    enabled = os.getenv('WEB_ENABLED', '0').strip().lower()
    if enabled not in ('0', 'false', 'no', '', '1', 'true', 'yes'):
        raise ValueError('WEB_ENABLED должен быть 0 или 1.')
    if enabled in ('1', 'true', 'yes'):
        service = WebsiteService(application.bot_data['store'], application.bot, application.bot_data['admins'])
        await service.start()
        application.bot_data['website_service'] = service


async def stop_website(application):
    service = application.bot_data.pop('website_service', None)
    if service:
        await service.stop()


async def close_storage(application):
    await stop_website(application)
    application.bot_data['store'].db.close()
