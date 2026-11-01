import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from web_api import Settings, create_app, deliver_notifications
from web_leads import CONSENT_VERSION, LeadStore


class WebApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(True, 'https://ricoellong.github.io', 'https://forms.example.test',
                                 'Тестовый оператор', 'Тестовый адрес, Россия', 'test@example.test',
                                 str(Path(self.temp.name) / 'leads.sqlite3'), 'not-a-real-token',
                                 ('99999',), 'admin', 'test-password-only-1234567890', 'ru')
        self.app = create_app(self.settings, notifications=False)
        self.client = TestClient(self.app)
        self.headers = {'Origin': self.settings.origin}
        self.body = {'submission_id': str(uuid4()), 'brand': 'Bosch', 'problem': 'Не сливает воду',
                     'name': 'Тест', 'phone': '8 (999) 000-00-00', 'comment': 'Проверка',
                     'consent': True, 'consent_version': CONSENT_VERSION, 'website': ''}

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def post(self, body=None):
        return self.client.post('/api/leads', json=body or self.body, headers=self.headers)

    def test_accept_persist_and_idempotent_retry(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        lead_id = response.json()['request_id']
        stored = LeadStore(self.settings.database).get(lead_id)
        self.assertEqual(stored['phone'], '+79990000000')
        self.assertEqual(stored['consent_version'], CONSENT_VERSION)
        self.assertTrue(stored['consent_at'])
        again = self.post()
        self.assertEqual(again.json()['request_id'], lead_id)
        self.assertEqual(len(self.app.state.store.pending()), 1)
        changed = dict(self.body, name='Другое имя')
        self.assertEqual(self.post(changed).status_code, 422)

    def test_same_phone_updates_callback(self):
        first = self.post().json()['request_id']
        second = self.post(dict(self.body, submission_id=str(uuid4()), problem='Не греет воду')).json()['request_id']
        self.assertEqual(first, second)
        self.assertEqual(self.app.state.store.get(first)['problem'], 'Не греет воду')

    def test_reject_missing_consent_phone_brand_and_honeypot(self):
        cases = [{'consent':False}, {'phone':'123'}, {'brand':'unknown'}, {'website':'spam'}, {'consent_version':'old'}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual(self.post(dict(self.body, **changes)).status_code, 422)
        with self.app.state.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM website_leads').fetchone()[0], 0)

    def test_missing_operator_or_disabled_never_collects(self):
        for changes in ({'enabled':False},{'operator_name':''},{'location':''},{'token':''},{'admin_password':'short'}):
            app = create_app(replace(self.settings, **changes), notifications=False)
            with TestClient(app) as client:
                self.assertFalse(client.get('/api/config').json()['ready'])
                self.assertEqual(client.post('/api/leads', json=self.body, headers=self.headers).status_code, 503)

    def test_origin_body_limit_and_rate_limit(self):
        denied = self.client.post('/api/leads', json=self.body, headers={'Origin':'https://evil.example'})
        self.assertEqual(denied.status_code,403)
        large = self.client.post('/api/leads',content=b'x'*17000,headers={**self.headers,'Content-Type':'application/json'})
        self.assertEqual(large.status_code,413)
        for _ in range(4): self.post(dict(self.body, submission_id=str(uuid4())))
        self.assertEqual(self.post().status_code,429)

    def test_protected_card_escapes_data_and_has_no_cache(self):
        lead_id = self.post(dict(self.body,name='<script>alert(1)</script>')).json()['request_id']
        url = '/admin/leads/'+lead_id
        self.assertEqual(self.client.get(url).status_code,401)
        response = self.client.get(url,auth=('admin',self.settings.admin_password))
        self.assertEqual(response.status_code,200)
        self.assertIn('&lt;script&gt;',response.text)
        self.assertNotIn('<script>alert',response.text)
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual(response.headers['x-frame-options'],'DENY')

    def test_outbox_retry_and_telegram_contains_no_contact_data(self):
        lead_id = self.post().json()['request_id']
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=MagicMock(is_success=False))
        with patch('web_api.httpx.AsyncClient',return_value=client):
            asyncio.run(deliver_notifications(self.settings,self.app.state.store))
        self.assertEqual(len(self.app.state.store.pending()),1)
        client.post = AsyncMock(return_value=MagicMock(is_success=True,json=lambda:{'ok':True}))
        with patch('web_api.httpx.AsyncClient',return_value=client):
            asyncio.run(deliver_notifications(self.settings,self.app.state.store))
        self.assertEqual(len(self.app.state.store.pending()),0)
        message=client.post.call_args.kwargs['json']['text']
        self.assertIn(lead_id,message)
        for forbidden in ('+79990000000','Тест','Проверка','Bosch'):
            self.assertNotIn(forbidden,message)

    def test_delivery_retry_skips_successful_admins(self):
        self.post()
        settings=replace(self.settings,admin_ids=('99999','88888'))
        client=MagicMock()
        client.__aenter__=AsyncMock(return_value=client)
        client.__aexit__=AsyncMock(return_value=False)
        good=MagicMock(is_success=True,json=lambda:{'ok':True})
        bad=MagicMock(is_success=False)
        client.post=AsyncMock(side_effect=[good,bad,good])
        with patch('web_api.httpx.AsyncClient',return_value=client):
            asyncio.run(deliver_notifications(settings,self.app.state.store))
            asyncio.run(deliver_notifications(settings,self.app.state.store))
        recipients=[call.kwargs['json']['chat_id'] for call in client.post.call_args_list]
        self.assertEqual(recipients,['99999','88888','88888'])
        self.assertEqual(self.app.state.store.pending(),[])

    def test_cors_preflight_and_wrong_content_type(self):
        response=self.client.options('/api/leads',headers={**self.headers,
            'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'Content-Type'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['access-control-allow-origin'],self.settings.origin)
        response=self.client.post('/api/leads',content='not-json',headers={**self.headers,'Content-Type':'text/plain'})
        self.assertEqual(response.status_code,415)

    def test_expiration_and_manual_erasure(self):
        lead_id = self.post().json()['request_id']
        with self.app.state.store.connect() as db:
            db.execute("UPDATE website_leads SET updated_at='2000-01-01T00:00:00+00:00'")
        self.app.state.store.purge(30)
        self.assertIsNone(self.app.state.store.get(lead_id))
        self.assertEqual(self.app.state.store.pending(),[])
        lead_id = self.post(dict(self.body,submission_id=str(uuid4()))).json()['request_id']
        self.assertTrue(self.app.state.store.delete(lead_id))
        self.assertIsNone(self.app.state.store.get(lead_id))


if __name__ == '__main__': unittest.main()
