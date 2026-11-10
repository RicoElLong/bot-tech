"""Server-rendered owner console, served by the existing aiohttp application."""

import asyncio
import hmac
import re
import secrets
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID, uuid4

from aiohttp import web
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from jinja2 import Environment, FileSystemLoader, select_autoescape

from admin_store import AdminConflict, AdminStore, RequestMissing, SOURCES, STATUSES, clean_text
from scheduling import SAMARA, TIMES, booking_days, local_now, slot_start
from site_content import SERVICES, SiteEditor, from_form
from storage import SlotUnavailable, canonical_phone


ROOT = Path(__file__).resolve().parent / 'admin_ui'
SESSION = '__Host-byttech_admin'
LOGIN_TOKEN = '__Host-byttech_login'
ADMIN_SESSION = web.RequestKey('admin_session')
ADMIN_FORM = web.RequestKey('admin_form')
PASSWORDS = PasswordHasher()


def date_value(raw):
    day = date.fromisoformat(raw)
    if day.isoformat() != raw:
        raise ValueError('Укажите дату в календаре.')
    return day


def display_time(value):
    if not value:
        return '—'
    parsed = datetime.fromisoformat(value)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(SAMARA).strftime('%d.%m.%Y, %H:%M')


def day_boundary(day):
    return datetime.combine(day, time.min, SAMARA).astimezone(timezone.utc).isoformat()


def same_token(expected, supplied):
    return bool(expected and supplied) and hmac.compare_digest(expected.encode(), supplied.encode())


def redirect(target):
    return web.Response(status=303, headers={'Location': target})


def set_cookie(response, name, value, age):
    response.set_cookie(name, value, max_age=age, path='/', secure=True, httponly=True, samesite='Strict')


async def form_data(request):
    if request.content_type != 'application/x-www-form-urlencoded':
        raise web.HTTPUnsupportedMediaType()
    return await asyncio.wait_for(request.post(), timeout=10)


class AdminWeb:
    def __init__(self, website, limiter, ttl=8 * 3600):
        self.website = website
        self.store = AdminStore(website.db)
        self.site = SiteEditor(self.store, website.web_root / 'index.html')
        self.limiter = limiter
        self.ttl = ttl
        self.password_slots = asyncio.Semaphore(2)
        self.templates = Environment(loader=FileSystemLoader(ROOT), autoescape=select_autoescape())
        self.templates.globals.update(statuses=STATUSES, sources=SOURCES, services=SERVICES)
        self.templates.filters['local_time'] = display_time

    def register(self, app):
        @web.middleware
        async def guard(request, handler):
            path = request.path
            if path != '/admin' and not path.startswith('/admin/'):
                return await handler(request)
            if not self.website.secure_admin_request(request):
                return web.Response(status=403, text='Админ-панель доступна через HTTPS.')
            if path in ('/admin/login', '/admin/style.css', '/admin/forms.js'):
                return await handler(request)
            session = self.store.session(request.cookies.get(SESSION, ''))
            if session is None:
                if request.method in ('GET', 'HEAD') and not path.startswith('/admin/api/'):
                    return redirect('/admin/login')
                return web.json_response({'error': 'Требуется вход.'}, status=401)
            request[ADMIN_SESSION] = session
            if request.method not in ('GET', 'HEAD'):
                try:
                    form = await form_data(request)
                except asyncio.TimeoutError:
                    return web.json_response({'error': 'Время запроса истекло.'}, status=408)
                if not same_token(session['csrf'], form.get('csrf', '')):
                    return self.page(request, 'error.html', status=403,
                                     message='Проверка запроса не пройдена. Обновите страницу.')
                request[ADMIN_FORM] = form
            return await handler(request)

        app.middlewares.append(guard)
        app.add_routes([
            web.get('/admin/login', self.login_page), web.post('/admin/login', self.login),
            web.post('/admin/logout', self.logout), web.get('/admin/style.css', self.stylesheet),
            web.get('/admin/forms.js', self.form_script),
            web.get('/admin', self.dashboard), web.get('/admin/requests', self.requests),
            web.post('/admin/requests/search', self.search),
            web.get('/admin/api/requests', self.requests_api),
            web.get('/admin/requests/new', self.new_request), web.post('/admin/requests/new', self.create_request),
            web.get(r'/admin/requests/{id:[1-9][0-9]{0,17}}', self.request),
            web.post(r'/admin/requests/{id:[1-9][0-9]{0,17}}/{action:status|note|archive|restore|schedule|cancel-visit}',
                     self.mutate_request),
            web.get('/admin/agenda', self.agenda), web.get('/admin/site', self.settings),
            web.get('/admin/site/preview', self.preview),
            web.post('/admin/site/{action:save|publish}', self.update_settings),
        ])

    def page(self, request, template, status=200, **context):
        session = request.get(ADMIN_SESSION)
        text = self.templates.get_template(template).render(
            admin=session, path=request.path, **context)
        return web.Response(text=text, content_type='text/html', status=status)

    async def stylesheet(self, _):
        return web.FileResponse(ROOT / 'style.css', headers={'Content-Type': 'text/css; charset=utf-8'})

    async def form_script(self, _):
        return web.FileResponse(ROOT / 'forms.js', headers={'Content-Type': 'text/javascript; charset=utf-8'})

    def login_form(self, request, error='', status=200):
        token = secrets.token_urlsafe(32)
        response = self.page(request, 'login.html', status=status, token=token, error=error)
        set_cookie(response, LOGIN_TOKEN, token, 600)
        if status == 429:
            response.headers['Retry-After'] = '600'
        return response

    async def login_page(self, request):
        if self.store.session(request.cookies.get(SESSION, '')):
            return redirect('/admin')
        return self.login_form(request)

    async def login(self, request):
        try:
            form = await form_data(request)
        except asyncio.TimeoutError:
            return self.login_form(request, 'Время запроса истекло. Повторите вход.', 408)
        if not same_token(request.cookies.get(LOGIN_TOKEN, ''), form.get('csrf', '')):
            return self.login_form(request, 'Страница входа устарела. Повторите вход.', 403)
        if not self.limiter.allow(self.website.client_ip(request)) or self.password_slots.locked():
            return self.login_form(request, 'Слишком много попыток. Попробуйте позже.', 429)
        owner = self.store.owner()
        username, password = form.get('username', '').strip(), form.get('password', '')
        matches = False
        if owner is not None and len(username) <= 64 and 1 <= len(password) <= 256:
            async with self.password_slots:
                try:
                    matches = await asyncio.to_thread(PASSWORDS.verify, owner['password_hash'], password)
                except VerificationError:
                    pass
                except ValueError:
                    pass
        if not matches or not same_token(owner['username'], username):
            return self.login_form(request, 'Проверьте логин и пароль.', 401)
        token = self.store.start_session(owner, request.cookies.get(SESSION, ''), self.ttl)
        if token is None:
            return self.login_form(request, 'Данные для входа изменились. Повторите вход.', 401)
        response = redirect('/admin')
        set_cookie(response, SESSION, token, self.ttl)
        set_cookie(response, LOGIN_TOKEN, '', 0)
        return response

    async def logout(self, request):
        self.store.end_session(request.cookies.get(SESSION, ''))
        response = redirect('/admin/login')
        set_cookie(response, SESSION, '', 0)
        return response

    async def dashboard(self, request):
        today = local_now().date()
        return self.page(request, 'dashboard.html', today=today.strftime('%d.%m.%Y'),
                         counts=self.store.overview(today.isoformat()))

    def filters(self, query):
        filters = {key: query.get(key, '') for key in ('status', 'source', 'from', 'to')}
        if (filters['status'] and filters['status'] not in STATUSES
                or filters['source'] and filters['source'] not in SOURCES):
            raise ValueError('Выберите статус и источник из списка.')
        start = date_value(filters['from']) if filters['from'] else None
        end = date_value(filters['to']) if filters['to'] else None
        if start and end and start > end:
            raise ValueError('Начало периода должно быть раньше окончания.')
        if end == date.max:
            raise ValueError('Выберите более раннюю дату окончания.')
        filters['after'] = day_boundary(start) if start else None
        filters['before'] = day_boundary(end + timedelta(days=1)) if end else None
        filters['archive'] = query.get('archive') == '1'
        filters['needs_call'] = query.get('needs_call') == '1'
        return filters

    def request_list(self, request):
        filters = self.filters(request.query)
        filters['q'] = request[ADMIN_SESSION]['search'] if request.query.get('search') == '1' else ''
        raw_page = request.query.get('page', '1')
        page = max(1, int(raw_page)) if re.fullmatch(r'[0-9]{1,7}', raw_page) else 1
        rows, total, page, last = self.store.requests(filters, page)
        return filters, rows, total, page, last

    async def requests(self, request):
        try:
            filters, rows, total, page, last = self.request_list(request)
        except (ValueError, OverflowError):
            return self.page(request, 'error.html', status=400, message='Проверьте фильтры и даты.')
        query = {key: request.query[key] for key in ('search', 'status', 'source', 'from', 'to', 'archive', 'needs_call')
                 if key in request.query}
        def page_url(n):
            return '/admin/requests?' + urlencode({**query, 'page': n})
        return self.page(request, 'requests.html', filters=filters, rows=rows, total=total, page=page, last=last,
                         previous=page_url(page - 1) if page > 1 else None,
                         following=page_url(page + 1) if page < last else None)

    async def search(self, request):
        form = request[ADMIN_FORM]
        try:
            filters = self.filters(form)
            query = clean_text(form.get('q', ''), 100)
        except (ValueError, OverflowError):
            return self.page(request, 'error.html', status=400, message='Проверьте фильтры и даты.')
        params = {key: filters[key] for key in ('status', 'source', 'from', 'to') if filters[key]}
        params.update({key: '1' for key in ('archive', 'needs_call') if filters[key]})
        self.store.set_search(request.cookies[SESSION], query)
        if query:
            params['search'] = '1'
        return redirect('/admin/requests' + ('?' + urlencode(params) if params else ''))

    async def requests_api(self, request):
        try:
            _, rows, total, page, last = self.request_list(request)
        except (ValueError, OverflowError):
            return web.json_response({'error': 'Проверьте фильтры и даты.'}, status=400)
        fields = ('id', 'name', 'phone', 'problem', 'source', 'status', 'created', 'appointment_day', 'appointment_time')
        return web.json_response({'items': [{key: row[key] for key in fields} for row in rows],
                                  'total': total, 'page': page, 'pages': last})

    async def new_request(self, request):
        return self.page(request, 'new.html', token=str(uuid4()), values={})

    async def create_request(self, request):
        form, actor = request[ADMIN_FORM], request[ADMIN_SESSION]['username']
        token = form.get('token', '')
        try:
            if len(token) != 36 or str(UUID(token, version=4)) != token:
                raise ValueError('Форма устарела. Откройте новую заявку.')
            values = {
                'name': clean_text(form.get('name', ''), 100, required=True),
                'phone': clean_text(form.get('phone', ''), 40, required=True),
                'brand': clean_text(form.get('brand', ''), 100),
                'problem': clean_text(form.get('problem', ''), 500, required=True),
                'comment': clean_text(form.get('comment', ''), 2000),
            }
            if (not re.fullmatch(r'\+?[0-9 ()-]+', values['phone'])
                    or not 10 <= len(canonical_phone(values['phone'])) - 1 <= 15):
                raise ValueError('Введите корректный телефон из 10–15 цифр.')
            values['phone'] = canonical_phone(values['phone'])
            request_id = self.store.create_manual(values, token, actor)
        except ValueError as error:
            return self.page(request, 'new.html', status=409 if isinstance(error, AdminConflict) else 400,
                             error=str(error), token=token, values=form)
        return redirect(f'/admin/requests/{request_id}')

    def request_page(self, request, request_id, error='', status=200):
        try:
            row = self.store.request(request_id)
        except RequestMissing:
            return self.page(request, 'error.html', status=404, message='Заявка не найдена.')
        days = booking_days()
        preferred = row['appointment_day'] or row['preferred_date'] or days[0]
        current = local_now()
        end = (date_value(days[-1]) + timedelta(days=1)).isoformat()
        occupied = {(item['appointment_day'], item['appointment_time'])
                    for item in self.store.agenda(days[0], end) if item['id'] != request_id}
        slots = {day: [(clock, (day, clock) in occupied) for clock in TIMES
                       if slot_start(day, clock) > current] for day in days}
        preferred_clock = row['appointment_time'] or row['preferred_time']
        return self.page(request, 'request.html', status=status, row=row, history=self.store.history(request_id),
                         slots=slots, preferred=preferred,
                         preferred_clock=preferred_clock, error=error)

    async def request(self, request):
        return self.request_page(request, int(request.match_info['id']))

    async def mutate_request(self, request):
        request_id = int(request.match_info['id'])
        action = request.match_info['action']
        form, actor = request[ADMIN_FORM], request[ADMIN_SESSION]['username']
        try:
            if action == 'status':
                self.store.set_status(request_id, form.get('status', ''), actor)
            elif action == 'note':
                self.store.add_note(request_id, clean_text(form.get('note', ''), 1000, required=True), actor)
            elif action in ('archive', 'restore'):
                self.store.archive(request_id, action == 'archive', actor)
            elif action == 'schedule':
                day, _, clock = form.get('slot', '').partition('|')
                self.store.schedule(request_id, day, clock, actor)
            else:
                self.store.cancel_visit(request_id, actor)
        except RequestMissing:
            return self.page(request, 'error.html', status=404, message='Заявка не найдена.')
        except (AdminConflict, SlotUnavailable) as error:
            return self.request_page(request, request_id, str(error), 409)
        except ValueError:
            return self.request_page(request, request_id, 'Проверьте дату, время и значения полей.', 400)
        return redirect(f'/admin/requests/{request_id}')

    async def agenda(self, request):
        try:
            day = date_value(request.query['date']) if request.query.get('date') else local_now().date()
            if not 2000 <= day.year < 2100:
                raise ValueError
        except ValueError:
            return self.page(request, 'error.html', status=400, message='Выберите корректную дату.')
        week = request.query.get('view') == 'week'
        start = day - timedelta(days=day.weekday()) if week else day
        count = 7 if week else 1
        end = start + timedelta(days=count)
        groups = {(start + timedelta(days=i)).isoformat(): [] for i in range(count)}
        for row in self.store.agenda(start.isoformat(), end.isoformat()):
            groups[row['appointment_day']].append(row)
        return self.page(request, 'agenda.html', groups=groups, week=week, selected=day.isoformat(),
                         previous=(day - timedelta(days=count)).isoformat(), following=(day + timedelta(days=count)).isoformat())

    def settings_page(self, request, status=200, error='', submitted=None):
        draft, public, revision, published_at = self.site.read()
        return self.page(request, 'settings.html', status=status, draft=draft, public=public, revision=revision,
                         published_at=published_at, error=error, submitted=submitted or {})

    async def settings(self, request):
        return self.settings_page(request)

    async def update_settings(self, request):
        form, actor = request[ADMIN_FORM], request[ADMIN_SESSION]['username']
        action = request.match_info['action']
        try:
            revision = form.get('revision', '')
            if not re.fullmatch(r'[0-9]{1,16}', revision):
                raise ValueError('Обновите страницу настроек.')
            if action == 'save':
                self.site.save(from_form(form), int(revision), actor)
            else:
                self.site.publish(int(revision), actor)
        except ValueError as error:
            return self.settings_page(request, 409 if isinstance(error, AdminConflict) else 400,
                                      str(error), form if action == 'save' else None)
        return redirect('/admin/site')

    async def preview(self, request):
        draft, _, _, _ = self.site.read()
        if draft is None:
            return self.settings_page(request, 409, 'Подключите актуальный шаблон сайта.')
        return self.website.site_document('index.html', preview=draft)
