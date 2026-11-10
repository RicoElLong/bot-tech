import asyncio
from contextlib import closing
from datetime import timedelta
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from admin_cli import backup
from admin_store import AdminStore, digest
from admin_web import LOGIN_TOKEN, PASSWORDS, SESSION
from scheduling import local_now
from site_content import SERVICES
from storage import Store, now
from webapp import SiteIdentity, WebSettings, WebsiteService


PASSWORD = 'test-password-only-29'
HEADERS = {'X-Forwarded-Proto': 'https'}
ORIGIN = 'https://example.test'
OWNER_HASH = PASSWORDS.hash(PASSWORD)


def sample_index():
    parts = ['<html><head></head><body><!-- site:hours -->08:00–22:00<!-- /site:hours -->']
    for key in SERVICES:
        parts.append(f'<!-- site:{key}-price -->от 1\u00a0250 ₽<!-- /site:{key}-price -->'
                     f'<!-- site:{key}-description -->Описание<!-- /site:{key}-description -->')
    return ''.join(parts) + '</body></html>'


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = Store(root / 'test.sqlite3')
        (root / 'index.html').write_text(sample_index(), encoding='utf-8')
        identity = SiteIdentity('Тестовый мастер', '000000000000', 'Адрес для тестов',
                                '+79990001122', 'test@example.test')
        self.service = WebsiteService(self.db, None, {99}, WebSettings(origins=(ORIGIN,)),
                                      web_root=root, site_identity=identity, admin_enabled=True)
        self.admin = self.service.admin.store
        self.admin.set_owner('owner', OWNER_HASH, reset=False)
        self.server = TestServer(self.service.make_app())
        await self.server.start_server()
        jar = CookieJar(unsafe=True, treat_as_secure_origin=self.server.make_url('/').origin())
        self.client = TestClient(self.server, cookie_jar=jar)
        self.day = (local_now().date() + timedelta(days=2)).isoformat()

    async def asyncTearDown(self):
        await self.client.close()
        self.db.db.close()
        self.temp.cleanup()

    async def get(self, path, headers=HEADERS):
        return await self.client.get(path, headers=headers, allow_redirects=False)

    async def post(self, path, values):
        return await self.client.post(path, data=values, headers=HEADERS, allow_redirects=False)

    async def login(self):
        page = await self.get('/admin/login')
        token = re.search(r'name="csrf" value="([^"]+)"', await page.text())[1]
        self.assertIn(LOGIN_TOKEN, page.cookies)
        response = await self.post('/admin/login', {'csrf': token, 'username': 'owner', 'password': PASSWORD})
        self.assertEqual(response.status, 303)
        for flag in ('secure', 'httponly', 'samesite'):
            self.assertTrue(response.cookies[SESSION][flag])
        self.token = response.cookies[SESSION].value
        self.csrf = self.admin.session(self.token)['csrf']

    async def action(self, path, **fields):
        return await self.post(path, {'csrf': self.csrf, **fields})

    async def website(self):
        payload = {'submission_id': str(uuid4()), 'name': 'Тестовая заявка', 'phone': '+79990000001',
                   'brand': 'Bosch', 'problem': 'Тестовая неисправность', 'comment': '',
                   'website': '', 'personal_data_consent': True, 'consent_version': 'pd-2026-11-09-v1',
                   'preferred_date': self.day, 'preferred_time': '10:00'}
        first = await self.client.post('/api/requests', json=payload, headers={'Origin': ORIGIN})
        self.assertEqual(first.status, 201)
        result = await first.json()
        second = await self.client.post('/api/requests', json=payload, headers={'Origin': ORIGIN})
        self.assertEqual(second.status, 200)
        self.assertEqual(await second.json(), result)
        return result['request_id']

    async def test_access_requires_https_session_and_csrf(self):
        self.assertEqual((await self.get('/admin/login', {})).status, 403)
        self.assertEqual((await self.get('/admin')).headers['Location'], '/admin/login')
        self.assertEqual((await self.get('/admin/api/requests')).status, 401)
        self.assertEqual((await self.post('/admin/login', {'password': PASSWORD})).status, 403)
        await self.login()
        self.assertEqual((await self.get('/admin')).headers['Cache-Control'], 'no-store')
        self.assertEqual((await self.post('/admin/logout', {'csrf': 'wrong'})).status, 403)
        self.assertEqual((await self.action('/admin/logout')).status, 303)
        self.assertEqual((await self.get('/admin/api/requests')).status, 401)

    async def test_login_limits_and_password_reset_revokes_session(self):
        for _ in range(6):
            page = await self.get('/admin/login')
            token = page.cookies[LOGIN_TOKEN].value
            response = await self.post('/admin/login', {'csrf': token, 'username': 'owner',
                                                       'password': 'wrong-password'})
            if response.status == 429:
                break
            self.assertEqual(response.status, 401)
        self.assertEqual(response.status, 429)
        self.service.admin.limiter.entries.clear()
        await self.login()
        with self.admin.transaction():
            self.db.db.execute('UPDATE admin_sessions SET expires=0 WHERE token_hash=?', (digest(self.token),))
        self.assertEqual((await self.get('/admin/api/requests')).status, 401)
        await self.login()
        self.admin.set_owner('owner', OWNER_HASH, reset=True)
        self.assertEqual((await self.get('/admin/api/requests')).status, 401)

    async def test_website_request_visit_notes_archive_and_restore(self):
        request_id = await self.website()
        self.assertEqual(len(self.db.due_web_notifications()), 1)
        await self.login()
        path = f'/admin/requests/{request_id}'
        self.assertIn('Ещё не назначен', await (await self.get(path)).text())
        self.assertEqual((await self.action(path + '/schedule', slot=f'{self.day}|10:00')).status, 303)
        self.assertEqual(self.admin.request(request_id)['status'], 'scheduled')
        self.assertIn('Тестовая заявка', await (await self.get('/admin/agenda?date=' + self.day)).text())
        self.assertIn('Тестовая заявка', await (await self.get('/admin/agenda?date=' + self.day + '&view=week')).text())
        self.assertEqual((await self.action(path + '/note', note='<script>private</script>')).status, 303)
        card = await (await self.get(path)).text()
        self.assertIn('&lt;script&gt;private&lt;/script&gt;', card)
        self.assertNotIn('<script>private', card)
        self.assertEqual((await self.action(path + '/status', status='done')).status, 303)
        self.assertEqual((await self.action(path + '/archive')).status, 303)
        self.assertEqual((await (await self.get('/admin/api/requests')).json())['total'], 0)
        self.assertEqual((await (await self.get('/admin/api/requests?archive=1')).json())['total'], 1)
        self.assertEqual((await self.action(path + '/restore')).status, 303)
        self.assertEqual(self.admin.request(request_id)['status'], 'done')
        self.assertEqual(len(self.admin.history(request_id)), 5)

    async def test_phone_request_idempotency_and_search_outside_url(self):
        await self.login()
        fields = {'token': str(uuid4()), 'name': 'Тест Телефон', 'phone': '8 (999) 000-00-02',
                  'brand': 'LG', 'problem': 'Тестовый звонок'}
        first, second = await asyncio.gather(*(self.action('/admin/requests/new', **fields) for _ in range(2)))
        self.assertEqual(first.status, 303)
        self.assertEqual(first.headers['Location'], second.headers['Location'])
        for query in ('тЕСТ тЕЛЕФОН', fields['phone'], '№' + first.headers['Location'].split('/')[-1]):
            response = await self.action('/admin/requests/search', q=query, source='phone')
            self.assertEqual(response.headers['Location'], '/admin/requests?source=phone&search=1')
            result = await (await self.get('/admin/api/requests?source=phone&search=1')).json()
            self.assertEqual(result['total'], 1)
            self.assertEqual(result['items'][0]['source'], 'phone')
        altered = await self.action('/admin/requests/new', **{**fields, 'problem': 'Другая поломка'})
        self.assertEqual(altered.status, 409)

    async def test_slot_conflict_keeps_original_visit(self):
        one, two = await self.website(), await self.website()
        await self.login()
        await self.action(f'/admin/requests/{one}/schedule', slot=f'{self.day}|08:00')
        await self.action(f'/admin/requests/{two}/schedule', slot=f'{self.day}|10:00')
        responses = await asyncio.gather(*(self.action(f'/admin/requests/{item}/schedule',
                                           slot=f'{self.day}|12:00') for item in (one, two)))
        self.assertEqual(sorted(response.status for response in responses), [303, 409])
        for item, original, response in zip((one,two), ('08:00','10:00'), responses):
            self.assertEqual(self.admin.request(item)['appointment_time'], '12:00' if response.status == 303 else original)
        self.assertEqual(len(self.admin.agenda(self.day, (local_now().date()+timedelta(days=3)).isoformat())), 2)

    async def test_telegram_requests_and_existing_bookings_are_visible(self):
        self.db.db.execute('INSERT INTO users(id,name,first_seen,last_seen,phone) VALUES (?,?,?,?,?)',
                           (100, 'Тест Telegram', now(), now(), '+79990000003'))
        self.db.db.commit()
        request_id = self.db.submit(100)
        self.db.book(100, self.day, '10:00')
        await self.login()
        data = await (await self.get('/admin/api/requests?source=telegram')).json()
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['items'][0]['id'], request_id)
        self.assertEqual(data['items'][0]['status'], 'scheduled')
        self.assertEqual((await self.action(f'/admin/requests/{request_id}/cancel-visit')).status, 303)
        self.assertIsNone(self.db.request(request_id)['booking'])

    async def test_site_draft_is_private_until_publication_and_stale_write_rejected(self):
        await self.login()
        fields = {'revision': '0', 'opening': '09:00', 'closing': '20:00'}
        for key in SERVICES:
            fields.update({f'{key}-description': '<b>Тест</b>', f'{key}-price': '1990'})
        self.assertEqual((await self.action('/admin/site/save', **fields)).status, 303)
        self.assertEqual((await self.action('/admin/site/save', **fields)).status, 409)
        public = await (await self.get('/')).text()
        self.assertIn('08:00–22:00', public)
        preview = await self.get('/admin/site/preview')
        self.assertIn("form-action 'none'", preview.headers['Content-Security-Policy'])
        self.assertIn('09:00–20:00', await preview.text())
        self.assertEqual((await self.action('/admin/site/publish', revision='1')).status, 303)
        public = await (await self.get('/')).text()
        self.assertIn('от 1\u00a0990 ₽', public)
        self.assertIn('&lt;b&gt;Тест&lt;/b&gt;', public)
        self.assertNotIn('<b>Тест</b>', public)


class AdminMigrationTests(unittest.TestCase):
    def test_old_site_database_keeps_ids_consent_bookings_and_queue(self):
        with TemporaryDirectory() as directory:
            original = Path(directory) / 'old.sqlite3'
            snapshot = Path(directory) / 'snapshot.sqlite3'
            with closing(sqlite3.connect(original)) as db:
                db.executescript("""
                    CREATE TABLE requests(id INTEGER PRIMARY KEY,user_id INTEGER,phone TEXT NOT NULL,
                        problem TEXT NOT NULL,created TEXT NOT NULL,booking TEXT,
                        source TEXT NOT NULL DEFAULT 'telegram' CHECK(source IN ('telegram','website')),
                        contact_name TEXT NOT NULL DEFAULT '',brand TEXT NOT NULL DEFAULT '',
                        comment TEXT NOT NULL DEFAULT '',preferred_date TEXT,preferred_time TEXT,
                        consent_version TEXT,consent_recorded_at TEXT);
                    CREATE UNIQUE INDEX requests_phone_unique ON requests(phone) WHERE source='telegram';
                    CREATE TABLE appointments(id INTEGER PRIMARY KEY,request_id INTEGER NOT NULL,
                        day TEXT NOT NULL,clock TEXT NOT NULL,created TEXT NOT NULL,cancelled TEXT);
                    CREATE TABLE web_notifications(request_id INTEGER NOT NULL,admin_id INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt REAL NOT NULL DEFAULT 0,PRIMARY KEY(request_id,admin_id));
                """)
                stamp = now()
                db.execute("INSERT INTO requests VALUES (17,NULL,'+79990000001','Тест',?,'01.01.2030 10:00','website','Тест','LG','','2030-01-01','10:00','pd-2026-11-09-v1',?)",
                           (stamp,stamp))
                db.execute("INSERT INTO appointments VALUES (1,17,'2030-01-01','10:00',?,NULL)", (stamp,))
                db.execute('INSERT INTO web_notifications(request_id,admin_id) VALUES (17,99)')
                db.commit()
            backup(original, snapshot)
            with self.assertRaises(FileExistsError):
                backup(original, snapshot)
            for _ in range(2):
                store = Store(snapshot)
                with closing(store.db):
                    admin = AdminStore(store)
                    row = admin.request(17)
                    self.assertEqual(row['consent_version'], 'pd-2026-11-09-v1')
                    self.assertEqual(row['appointment_time'], '10:00')
                    self.assertEqual(row['status'], 'scheduled')
                    self.assertEqual(store.due_web_notifications()[0]['id'], 17)
            with closing(sqlite3.connect(original)) as old:
                schema = old.execute("SELECT sql FROM sqlite_master WHERE name='requests'").fetchone()[0]
                self.assertNotIn("'phone'", schema)
