import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import bot
from pathlib import Path
from bot import message, admin, admin_page, normalize_phone
from storage import Store


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite3'
        self.db = Store(self.path)
        self.user = SimpleNamespace(id=12, full_name='Клиент', username='client')
        self.db.visit(self.user)
        self.msg = SimpleNamespace(text='', contact=None, reply_text=AsyncMock())
        self.update = SimpleNamespace(message=self.msg, effective_user=self.user,
                                      effective_chat=SimpleNamespace(type='private'))
        self.context = SimpleNamespace(application=SimpleNamespace(bot_data={'store': self.db, 'admins': {99}}),
                                       bot=SimpleNamespace(send_message=AsyncMock()), user_data={})

    def tearDown(self):
        self.db.db.close()
        self.temp.cleanup()

    async def send(self, text):
        self.msg.text = text
        await message(self.update, self.context)

    async def test_full_flow_and_restart(self):
        await self.send('не телефон')
        self.assertEqual(self.db.user(12)['stage'], 'phone')
        await self.send('+7 (999) 123-45-67')
        self.assertEqual(self.db.user(12)['phone'], '+79991234567')
        await self.send('Bosch, не сливает воду')
        self.assertIsNone(self.db.latest(12))
        self.db.db.close()
        self.db = Store(self.path)
        self.context.application.bot_data['store'] = self.db
        await self.send('Далее')
        self.assertEqual(self.db.latest(12)['problem'], 'Bosch, не сливает воду')
        self.assertIn('+79991234567', self.context.bot.send_message.call_args.args[1])
        await self.send('Запись')
        await self.send('Завтра после 18:00')
        self.assertEqual(self.db.latest(12)['booking'], 'Завтра после 18:00')
        self.assertEqual(self.db.user(12)['stage'], 'menu')

    async def test_skip_problem(self):
        await self.send('89991234567')
        await self.send('Далее')
        self.assertEqual(self.db.latest(12)['problem'], 'Не указана')

    async def test_foreign_contact_rejected(self):
        self.msg.contact = SimpleNamespace(user_id=13, phone_number='+79991234567')
        await self.send('')
        self.assertIsNone(self.db.user(12)['phone'])

    async def test_admin_access(self):
        await admin(self.update, self.context)
        self.assertEqual(self.msg.reply_text.call_args.args[0], 'Доступ только для администратора.')
        query = SimpleNamespace(from_user=self.user, answer=AsyncMock(), edit_message_text=AsyncMock())
        await admin_page(SimpleNamespace(callback_query=query), self.context)
        query.edit_message_text.assert_not_called()
        self.user.id = 99
        await admin(self.update, self.context)
        self.assertIn('Админ-панель', self.msg.reply_text.call_args.args[0])

    async def test_custom_buttons_route_to_actions(self):
        self.addCleanup(bot.configure_buttons)
        with patch.dict('os.environ', {'BUTTON_NEXT': 'Продолжить', 'BUTTON_BOOKING': 'Вызвать мастера'}):
            bot.configure_buttons()
            await self.send('89991234567')
            self.assertIn('Продолжить', self.msg.reply_text.call_args.args[0])
            self.assertEqual(bot.NEXT.keyboard[0][0].text, 'Продолжить')
            await self.send('Не отжимает')
            await self.send('Продолжить')
            self.assertEqual(self.db.latest(12)['problem'], 'Не отжимает')
            await self.send('Вызвать мастера')
            self.assertEqual(self.db.user(12)['stage'], 'booking')
            await self.send('Понедельник, 10:00')
            self.assertEqual(self.db.latest(12)['booking'], 'Понедельник, 10:00')
        bot.configure_buttons()

    def test_phone_validation(self):
        for value in ['123', 'hello1234567890', '+1234567890123456', '']:
            self.assertIsNone(normalize_phone(value))


if __name__ == '__main__':
    unittest.main()
