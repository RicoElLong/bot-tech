import asyncio
import json
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from datetime import timedelta
from scheduling import local_now

from aiohttp.test_utils import TestClient, TestServer
from telegram.error import NetworkError, RetryAfter

import bot
from storage import Store
from webapp import (WebSettings, WebsiteService, SiteIdentity, deliver_notifications,
                    start_website, stop_website, validated_submission)


class WebsiteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite3'
        self.db = Store(self.path)
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.settings = WebSettings(origins=('https://byttech.example',), trusted_proxies=())
        self.identity = SiteIdentity(name='Тестовый Мастер', inn='123456789012',
                                     address='Адрес для тестов', phone='+79990001122',
                                     email='testing@example.test')
        self.service = WebsiteService(self.db, self.bot, {99, 100}, self.settings,
                                      site_identity=self.identity)
        self.client = TestClient(TestServer(self.service.make_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.db.db.close()
        self.temp.cleanup()

    def payload(self, **changes):
        data = {'submission_id': str(uuid4()), 'name': 'Анна', 'phone': '8 (999) 123-45-67',
                'brand': 'Bosch', 'problem': 'Не сливает воду', 'comment': 'Вечером после 18:00', 'website': '',
                'preferred_date': (local_now().date()+timedelta(days=2)).isoformat(), 'preferred_time': '10:00',
                'personal_data_consent': True, 'consent_version': 'pd-2026-11-09-v1'}
        data.update(changes)
        return data

    async def post(self, data, **kwargs):
        return await self.client.post('/api/requests', json=data,
                                      headers={'Origin': self.settings.origins[0], **kwargs})

    async def test_saved_without_telegram_and_visible_to_admin(self):
        response = await self.post(self.payload())
        self.assertEqual(response.status, 201)
        result = await response.json()
        self.assertTrue(result['accepted'])
        row = self.db.request(result['request_id'])
        self.assertIsNone(row['user_id'])
        self.assertEqual(row['phone'], '+79991234567')
        self.assertEqual(row['source'], 'website')
        self.assertEqual(row['preferred_date'], self.payload()['preferred_date'])
        self.assertEqual(row['preferred_time'], '10:00')
        self.assertEqual(row['consent_version'], 'pd-2026-11-09-v1')
        self.assertIsNotNone(row['consent_recorded_at'])
        self.assertEqual(self.db.counts(), (0, 0, 1))
        query = SimpleNamespace(from_user=SimpleNamespace(id=99), data='admin:requests:0',
                                answer=AsyncMock(), edit_message_text=AsyncMock())
        context = SimpleNamespace(application=SimpleNamespace(bot_data={'store': self.db, 'admins': {99}}))
        await bot.admin_page(SimpleNamespace(callback_query=query), context)
        text = query.edit_message_text.call_args.args[0]
        self.assertIn('Анна', text)
        self.assertIn('Источник: сайт', text)
        self.assertIn('Bosch', text)
        self.assertIn('Желаемое время', text)
        self.assertIn('10:00', text)
        self.assertNotIn('ID None', text)
        self.bot.send_message.assert_not_awaited()


    async def test_preferred_time_validation(self):
        day = (local_now().date()+timedelta(days=2)).isoformat()
        for payload in (
            self.payload(preferred_date=day,preferred_time='04:00'),
            self.payload(preferred_date=day,preferred_time='22:00'),
            self.payload(preferred_date='2000-01-01',preferred_time='12:00'),
            self.payload(preferred_date=day,preferred_time=''),
        ):
            response=await self.post(payload)
            self.assertEqual(response.status,400)
        response=await self.post(self.payload(preferred_date=day,preferred_time='16:00'))
        self.assertEqual(response.status,201)
        self.assertEqual(self.db.page('requests',0)[0]['preferred_time'],'16:00')

    async def test_retry_and_restart_do_not_duplicate_request_or_queue(self):
        data = self.payload()
        first = await (await self.post(data)).json()
        self.db.db.close()
        self.db = Store(self.path)
        self.service.db = self.db
        for _ in range(7):
            response = await self.post(data)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), first)
        self.assertEqual(self.db.counts()[2], 1)
        self.assertEqual(len(self.db.due_web_notifications()), 2)

    async def test_reusing_submission_id_with_changed_data_is_conflict(self):
        data = self.payload()
        await self.post(data)
        response = await self.post({**data, 'problem': 'Другой текст'})
        self.assertEqual(response.status, 409)
        self.assertEqual(self.db.page('requests', 0)[0]['problem'], data['problem'])

    async def test_website_cannot_overwrite_telegram_request_or_change_its_booking(self):
        user = SimpleNamespace(id=12, full_name='Клиент бота', username='client')
        self.db.visit(user)
        self.db.phone(12, '+79991234567')
        self.db.draft(12, 'Описание из Telegram')
        original = self.db.submit(12)
        for _ in range(2):
            response = await self.post(self.payload())
            self.assertEqual(response.status, 201)
        self.assertEqual(self.db.latest(12)['id'], original)
        self.assertEqual(self.db.latest(12)['problem'], 'Описание из Telegram')
        self.assertEqual(self.db.counts()[2], 3)
        self.assertEqual(self.db.user(12)['stage'], 'booking_date')
        web_id = self.db.page('requests', 0)[0]['id']
        self.assertTrue(self.db.delete_request(web_id))
        self.assertEqual(self.db.user(12)['stage'], 'booking_date')
        self.assertEqual(self.db.latest(12)['id'], original)

    async def test_each_admin_delivery_is_retried_independently_after_restart(self):
        await self.post(self.payload())

        async def send(**kwargs):
            if kwargs['chat_id'] == 99:
                raise NetworkError('temporary')

        self.bot.send_message.side_effect = send
        await deliver_notifications(self.db, self.bot, {99, 100})
        statuses = self.db.db.execute('SELECT admin_id,status,attempts FROM web_notifications ORDER BY admin_id').fetchall()
        self.assertEqual([tuple(row) for row in statuses], [(99, 'pending', 1), (100, 'sent', 0)])
        self.db.db.close()
        self.db = Store(self.path)
        self.service.db = self.db
        self.assertEqual(self.db.counts()[2], 1)
        with self.db.db:
            self.db.db.execute('UPDATE web_notifications SET next_attempt=0')
        self.bot.send_message.reset_mock()
        self.bot.send_message.side_effect = None
        await deliver_notifications(self.db, self.bot, {99, 100})
        self.bot.send_message.assert_awaited_once()
        kwargs = self.bot.send_message.call_args.kwargs
        self.assertEqual(kwargs['chat_id'], 99)
        self.assertIsNone(kwargs['parse_mode'])
        self.assertIn('Источник: сайт', kwargs['text'])
        self.assertEqual(len(self.db.due_web_notifications()), 0)

    async def test_notification_honors_telegram_retry_after(self):
        await self.post(self.payload())
        self.bot.send_message.side_effect = RetryAfter(60)
        await deliver_notifications(self.db, self.bot, {99, 100})
        self.assertEqual(len(self.db.due_web_notifications()), 0)
        rows = self.db.db.execute('SELECT attempts FROM web_notifications').fetchall()
        self.assertEqual([row[0] for row in rows], [1, 1])

    async def test_removed_admin_does_not_receive_queued_personal_data(self):
        await self.post(self.payload())
        await deliver_notifications(self.db, self.bot, {99})
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.call_args.kwargs['chat_id'], 99)
        self.assertEqual(self.db.db.execute("SELECT status FROM web_notifications WHERE admin_id=100").fetchone()[0], 'cancelled')

    async def test_deleted_request_is_not_recreated_by_late_browser_retry(self):
        data = self.payload()
        first = await (await self.post(data)).json()
        self.db.delete_request(first['request_id'])
        response = await self.post(data)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), first)
        self.assertEqual(self.db.counts()[2], 0)
        self.assertEqual(len(self.db.due_web_notifications()), 0)
        next_result = await (await self.post(self.payload())).json()
        self.assertGreater(next_result['request_id'], first['request_id'])

    async def test_origin_and_json_are_required(self):
        for headers in ({}, {'Origin': 'https://attacker.example'}, {'Origin': 'null'}):
            response = await self.client.post('/api/requests', json=self.payload(), headers=headers)
            self.assertEqual(response.status, 403)
            self.assertNotIn('Access-Control-Allow-Origin', response.headers)
        response = await self.client.post('/api/requests', data='{}', headers={'Origin': self.settings.origins[0]})
        self.assertEqual(response.status, 415)
        response = await self.client.options('/api/requests', headers={'Origin': self.settings.origins[0]})
        self.assertEqual(response.status, 204)
        self.assertEqual(response.headers['Access-Control-Allow-Origin'], self.settings.origins[0])
        self.assertEqual(self.db.counts()[2], 0)

    async def test_invalid_fields_and_honeypot_never_create_request(self):
        for data in (self.payload(phone='123'), self.payload(name=''), self.payload(website='spam'),
                     self.payload(personal_data_consent=False),
                     self.payload(personal_data_consent='true'),
                     self.payload(consent_version='unknown'),
                     self.payload(consent_version=''),
                     self.payload(submission_id='bad'), self.payload(name='Анна\nАдмин'),
                     self.payload(comment='x' * 2001), [], self.payload(phone=True)):
            with self.subTest(data=type(data).__name__):
                response = await self.post(data)
                self.assertEqual(response.status, 400)
        self.assertEqual(self.db.counts()[2], 0)

    async def test_unicode_cannot_make_notification_exceed_telegram_limit(self):
        data = self.payload(name='😀' * 100, problem='😀' * 200, comment='😀' * 1600)
        response = await self.client.post('/api/requests', data=json.dumps(data, ensure_ascii=False),
            headers={'Origin': self.settings.origins[0], 'Content-Type': 'application/json'})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.db.counts()[2], 0)

    async def test_oversized_or_malformed_json_never_create_request(self):
        headers = {'Origin': self.settings.origins[0], 'Content-Type': 'application/json'}
        response = await self.client.post('/api/requests', data='{' + 'x' * 13000, headers=headers)
        self.assertEqual(response.status, 413)
        response = await self.client.post('/api/requests', data='{', headers=headers)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.db.counts()[2], 0)

    async def test_rate_limit_cannot_be_bypassed_with_untrusted_forwarded_header(self):
        for i in range(5):
            self.assertEqual((await self.post(self.payload(), **{'X-Real-IP': f'192.0.2.{i+1}'})).status, 201)
        response = await self.post(self.payload(), **{'X-Real-IP': '192.0.2.99'})
        self.assertEqual(response.status, 429)
        self.assertEqual(self.db.counts()[2], 5)

    async def test_known_reverse_proxy_uses_client_ip(self):
        import ipaddress
        self.service.proxies = (ipaddress.ip_network('127.0.0.1/32'),)
        for _ in range(5):
            self.assertEqual((await self.post(self.payload(), **{'X-Real-IP': '192.0.2.1'})).status, 201)
        response = await self.post(self.payload(), **{'X-Real-IP': '192.0.2.2'})
        self.assertEqual(response.status, 201)

    async def test_storage_failure_returns_error_instead_of_success(self):
        self.db.db.close()
        response = await self.post(self.payload())
        self.assertEqual(response.status, 503)
        self.db = Store(self.path)
        self.service.db = self.db
        self.assertEqual(self.db.counts()[2], 0)

    async def test_queue_failure_rolls_back_the_entire_submission(self):
        self.db.db.execute('''CREATE TRIGGER fail_notification BEFORE INSERT ON web_notifications
            BEGIN SELECT RAISE(ABORT,'simulated storage failure'); END''')
        response = await self.post(self.payload())
        self.assertEqual(response.status, 503)
        self.assertEqual(self.db.counts()[2], 0)
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM web_submissions').fetchone()[0], 0)

    def test_existing_bookings_survive_source_migration(self):
        legacy_path = Path(self.temp.name) / 'legacy-bookings.sqlite3'
        with sqlite3.connect(legacy_path) as db:
            db.executescript('''
                CREATE TABLE requests(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,
                    phone TEXT NOT NULL,problem TEXT NOT NULL,created TEXT NOT NULL,booking TEXT);
                CREATE UNIQUE INDEX requests_phone_unique ON requests(phone);
                INSERT INTO requests VALUES(7,12,'+79991234567','Не греет','2026-10-30','02.11.2026 10:00');
                CREATE TABLE appointments(id INTEGER PRIMARY KEY,request_id INTEGER NOT NULL,
                    day TEXT NOT NULL,clock TEXT NOT NULL,created TEXT NOT NULL,cancelled TEXT);
                INSERT INTO appointments VALUES(3,7,'2026-11-02','10:00','2026-10-30',NULL);
            ''')
        legacy = Store(legacy_path)
        try:
            self.assertEqual(legacy.request(7)['booking'], '02.11.2026 10:00')
            submission_id, digest, data = validated_submission(self.payload())
            request_id, _ = legacy.submit_website(submission_id, digest, data, {99})
            self.assertGreater(request_id, 7)
        finally:
            legacy.db.close()
        reopened = Store(legacy_path)
        try:
            self.assertEqual(reopened.request(7)['source'], 'telegram')
            appointment = reopened.db.execute('SELECT * FROM appointments WHERE id=3').fetchone()
            self.assertEqual(appointment['request_id'], 7)
            self.assertIsNone(appointment['cancelled'])
            self.assertEqual(reopened.request(request_id)['source'], 'website')
        finally:
            reopened.db.close()

    async def test_only_explicit_public_files_are_served(self):
        response = await self.client.get('/')
        self.assertEqual(response.status, 200)
        html = await response.text()
        self.assertNotIn('BOT_TOKEN', html)
        self.assertNotIn('api.telegram.org', html)
        self.assertIn('/request-form.js', html)
        self.assertIn('/consent', html)
        self.assertIn('/privacy', html)
        self.assertIn('Тестовый Мастер', html)
        self.assertNotIn('{{OWNER_NAME}}', html)
        self.assertNotIn('https://fonts.googleapis.com', html)
        self.assertNotIn('t.me/oleg_samara', html)
        for path in ('/privacy', '/consent', '/legal'):
            page = await self.client.get(path)
            self.assertEqual(page.status, 200)
            self.assertEqual(page.headers.get('X-Frame-Options'), 'DENY')
            body = await page.text()
            self.assertIn('Тестовый Мастер', body)
            self.assertIn('testing@example.test', body)
            self.assertNotIn('{{OWNER_NAME}}', body)
        for path in ('/.env', '/bot.py', '/data/bot.sqlite3'):
            self.assertEqual((await self.client.get(path)).status, 404)

    async def test_service_lifecycle_cleans_up_background_worker(self):
        service = WebsiteService(self.db, self.bot, {99}, WebSettings(port=0))
        await service.start()
        await asyncio.sleep(0)
        await service.stop()
        self.assertTrue(service.task.done())

    async def test_disabled_web_does_not_start_a_server(self):
        application = SimpleNamespace(bot_data={})
        with patch.dict(os.environ, {'WEB_ENABLED': '0'}):
            await start_website(application)
        await stop_website(application)
        self.assertEqual(application.bot_data, {})

    def test_public_identity_is_required_for_launch(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            SiteIdentity.from_env()
        fake = {
            'WEB_OWNER_FULL_NAME': 'Иван Иванов',
            'WEB_OWNER_INN': '123456789012',
            'WEB_OWNER_ADDRESS': 'Тестовый почтовый адрес',
            'WEB_CONTACT_PHONE': '+79990001122',
            'WEB_CONTACT_EMAIL': 'test@example.test',
            'WEB_SITE_PRICES_CONFIRMED': '1',
            'WEB_DATA_STORAGE_RU_CONFIRMED': '1',
            'WEB_TELEGRAM_TRANSFER_REVIEWED': '1',
        }
        with patch.dict(os.environ, fake, clear=True):
            self.assertEqual(SiteIdentity.from_env().name, 'Иван Иванов')
        for name in ('WEB_SITE_PRICES_CONFIRMED', 'WEB_DATA_STORAGE_RU_CONFIRMED',
                     'WEB_TELEGRAM_TRANSFER_REVIEWED'):
            bad = {**fake, name: '0'}
            with patch.dict(os.environ, bad, clear=True), self.assertRaises(ValueError):
                SiteIdentity.from_env()
        with patch.dict(os.environ, {**fake, 'WEB_CONTACT_PHONE': '123'},
                        clear=True), self.assertRaises(ValueError):
            SiteIdentity.from_env()

    async def test_unconfigured_public_site_is_unavailable(self):
        service = WebsiteService(self.db, self.bot, {99}, self.settings)
        for page in ('index.html', 'privacy.html', 'consent.html', 'legal.html'):
            with self.assertRaises(Exception):
                service.site_document(page)

    def test_intake_requires_admin_and_valid_configuration(self):
        with self.assertRaises(ValueError):
            WebsiteService(self.db, self.bot, set())
        for values in ({'WEB_ALLOWED_ORIGINS': '*'}, {'WEB_ALLOWED_ORIGINS': 'https://example.com/path'},
                       {'WEB_PORT': '0'}):
            with patch.dict(os.environ, values), self.assertRaises(ValueError):
                WebSettings.from_env()


if __name__ == '__main__':
    unittest.main()
