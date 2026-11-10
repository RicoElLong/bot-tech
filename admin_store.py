"""Owner operations over the same SQLite store used by the bot."""

import hashlib
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from storage import SlotUnavailable, canonical_phone, now


STATUSES = {
    'new': 'Новая', 'in_progress': 'В работе', 'scheduled': 'Визит назначен',
    'done': 'Выполнена', 'cancelled': 'Отменена',
}
SOURCES = {'website': 'Сайт', 'telegram': 'Telegram', 'phone': 'Телефон'}
PAGE_SIZE = 25


class RequestMissing(LookupError):
    pass


class AdminConflict(ValueError):
    pass


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def clean_text(value, limit, required=False):
    value = value.strip()
    if len(value) > limit or (required and not value) or re.search(r'[\x00-\x08\x0b-\x1f\x7f]', value):
        raise ValueError('Проверьте заполнение и длину полей.')
    return value


class AdminStore:
    def __init__(self, store):
        self.store = store
        self.db = store.db
        self.db.create_function('admin_casefold', 1, str.casefold, deterministic=True)
        self.db.create_function('admin_digits', 1, lambda value: re.sub(r'\D', '', value), deterministic=True)
        self.db.executescript('''
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS admin_owner (
                id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, password_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS admin_sessions (
                token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires INTEGER NOT NULL,
                search TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS admin_request_state (
                request_id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'new'
                    CHECK(status IN ('new','in_progress','done','cancelled')),
                updated_at TEXT NOT NULL DEFAULT '', archived_at TEXT);
            CREATE TABLE IF NOT EXISTS admin_events (
                id INTEGER PRIMARY KEY, request_id INTEGER, actor TEXT NOT NULL,
                action TEXT NOT NULL, detail TEXT NOT NULL, created TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS admin_events_request
                ON admin_events(request_id, id DESC);
            CREATE TABLE IF NOT EXISTS admin_manual_submissions (
                token TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, request_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS admin_site_content (
                id INTEGER PRIMARY KEY CHECK(id=1), draft TEXT, published TEXT,
                revision INTEGER NOT NULL DEFAULT 0, published_at TEXT);
            CREATE TRIGGER IF NOT EXISTS admin_requests_cleanup AFTER DELETE ON requests BEGIN
                DELETE FROM admin_request_state WHERE request_id=OLD.id;
                DELETE FROM admin_events WHERE request_id=OLD.id;
            END;
            CREATE VIEW IF NOT EXISTS admin_requests AS
                SELECT r.*, COALESCE(NULLIF(r.contact_name,''), u.name,
                       CASE r.source WHEN 'phone' THEN 'Телефонное обращение' ELSE 'Посетитель сайта' END) AS name,
                       a.day AS appointment_day, a.clock AS appointment_time,
                       s.archived_at,
                       CASE
                           WHEN a.id IS NOT NULL AND a.created > COALESCE(s.updated_at,'') THEN 'scheduled'
                           WHEN s.status IN ('done','cancelled') THEN s.status
                           WHEN a.id IS NOT NULL OR NULLIF(r.booking,'') IS NOT NULL THEN 'scheduled'
                           ELSE COALESCE(s.status,'new')
                       END AS status
                FROM requests r
                LEFT JOIN users u ON u.id=r.user_id
                LEFT JOIN appointments a ON a.request_id=r.id AND a.cancelled IS NULL
                LEFT JOIN admin_request_state s ON s.request_id=r.id;
            COMMIT;
        ''')

    @contextmanager
    def transaction(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            yield

    def owner(self):
        return self.db.execute('SELECT username,password_hash FROM admin_owner WHERE id=1').fetchone()

    def set_owner(self, username, password_hash, *, reset):
        with self.transaction():
            exists = self.owner() is not None
            if exists != reset:
                raise ValueError('Используйте reset-owner для смены пароля.' if exists
                                 else 'Сначала выполните create-owner.')
            self.db.execute('INSERT OR REPLACE INTO admin_owner VALUES (1,?,?)', (username, password_hash))
            self.db.execute('DELETE FROM admin_sessions')

    def start_session(self, owner, old_token, ttl):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        timestamp = int(time.time())
        with self.transaction():
            current = self.owner()
            if current is None or tuple(current) != tuple(owner):
                return None
            self.db.execute('DELETE FROM admin_sessions WHERE expires<=? OR token_hash=?',
                            (timestamp, digest(old_token)))
            self.db.execute('INSERT INTO admin_sessions(token_hash,csrf,expires) VALUES (?,?,?)',
                            (digest(token), csrf, timestamp + ttl))
        return token

    def session(self, token):
        return self.db.execute('''SELECT o.username, s.csrf, s.search FROM admin_sessions s
            JOIN admin_owner o ON o.id=1 WHERE s.token_hash=? AND s.expires>?''',
            (digest(token), int(time.time()))).fetchone() if token else None

    def end_session(self, token):
        with self.db:
            self.db.execute('DELETE FROM admin_sessions WHERE token_hash=?', (digest(token),))

    def set_search(self, token, query):
        with self.db:
            self.db.execute('UPDATE admin_sessions SET search=? WHERE token_hash=?',
                            (query, digest(token)))

    def request(self, request_id):
        row = self.db.execute('SELECT * FROM admin_requests WHERE id=?', (request_id,)).fetchone()
        if row is None:
            raise RequestMissing
        return row

    def event(self, actor, action, detail='', request_id=None):
        self.db.execute('INSERT INTO admin_events(request_id,actor,action,detail,created) VALUES(?,?,?,?,?)',
                        (request_id, actor, action, detail, now()))

    def history(self, request_id):
        return self.db.execute('SELECT * FROM admin_events WHERE request_id=? ORDER BY id DESC',
                               (request_id,)).fetchall()

    def requests(self, filters, page=1):
        where = ['archived_at IS NOT NULL' if filters.get('archive') else 'archived_at IS NULL']
        params = []
        query = filters.get('q', '')
        if query:
            clauses = ['instr(admin_casefold(name),?)>0']
            params.append(query.casefold())
            number = query.lstrip('№#').strip()
            if re.fullmatch(r'[0-9]{1,18}', number):
                clauses.append('id=?')
                params.append(int(number))
            if re.fullmatch(r'[+0-9() -]+', query) and re.search(r'[0-9]', query):
                digits = re.sub(r'\D', '', query)
                clauses.append('instr(admin_digits(phone),?)>0')
                params.append(canonical_phone(query)[1:] if len(digits) == 11 else digits)
            where.append('(' + ' OR '.join(clauses) + ')')
        for field in ('status', 'source'):
            if filters.get(field):
                where.append(f'{field}=?')
                params.append(filters[field])
        if filters.get('after'):
            where.append('created>=?')
            params.append(filters['after'])
        if filters.get('before'):
            where.append('created<?')
            params.append(filters['before'])
        if filters.get('needs_call'):
            where.extend(("status='new'", 'created<=?'))
            params.append(self.call_cutoff())
        where_sql = ' AND '.join(where)
        total = self.db.execute(f'SELECT COUNT(*) FROM admin_requests WHERE {where_sql}', params).fetchone()[0]
        last_page = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, last_page)
        rows = self.db.execute(f'SELECT * FROM admin_requests WHERE {where_sql} '
                               'ORDER BY created DESC,id DESC LIMIT ? OFFSET ?',
                               (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
        return rows, total, page, last_page

    @staticmethod
    def call_cutoff():
        return (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    def overview(self, day):
        summary = self.db.execute('''SELECT
            COUNT(CASE WHEN status='new' AND archived_at IS NULL THEN 1 END) AS new,
            COUNT(CASE WHEN appointment_day=? THEN 1 END) AS today,
            COUNT(CASE WHEN status='new' AND archived_at IS NULL AND created<=? THEN 1 END) AS needs_call
            FROM admin_requests''', (day, self.call_cutoff())).fetchone()
        return summary

    def agenda(self, start, end):
        return self.db.execute('''SELECT * FROM admin_requests WHERE appointment_day>=?
            AND appointment_day<? ORDER BY appointment_day,appointment_time''', (start, end)).fetchall()

    def create_manual(self, values, token, actor):
        payload_hash = digest(json.dumps(values, ensure_ascii=False, sort_keys=True))
        with self.transaction():
            previous = self.db.execute('SELECT * FROM admin_manual_submissions WHERE token=?', (token,)).fetchone()
            if previous:
                if previous['payload_hash'] != payload_hash or self.store.request(previous['request_id']) is None:
                    raise AdminConflict('Эта форма уже использована. Откройте новую заявку.')
                return previous['request_id']
            request_id = self.db.execute('''INSERT INTO requests
                (id,user_id,phone,problem,created,source,contact_name,brand,comment)
                VALUES((SELECT COALESCE(MAX(id),0)+1 FROM (
                    SELECT id FROM requests UNION ALL SELECT id FROM request_tombstones)),
                    NULL,?,?,?,'phone',?,?,?) RETURNING id''',
                (values['phone'], values['problem'], now(), values['name'],
                 values['brand'], values['comment'])).fetchone()[0]
            self.db.execute('INSERT INTO admin_manual_submissions VALUES(?,?,?)',
                            (token, payload_hash, request_id))
            self.event(actor, 'Создана по звонку', request_id=request_id)
        return request_id

    def set_status(self, request_id, status, actor):
        if status not in STATUSES or status == 'scheduled':
            raise ValueError('Выберите статус. Для назначения визита укажите дату и время.')
        with self.transaction():
            row = self.request(request_id)
            self.require_active(row)
            if status == row['status']:
                return
            if status in ('new', 'in_progress') and row['status'] == 'scheduled':
                raise AdminConflict('Сначала отмените визит или выберите статус «Выполнена».')
            if status == 'cancelled' and row['booking']:
                self.store.cancel_appointment(request_id)
                self.event(actor, 'Визит отменён', row['booking'], request_id)
            self.write_status(request_id, status)
            self.event(actor, 'Статус изменён',
                       f'{STATUSES[row["status"]]} → {STATUSES[status]}', request_id)

    def write_status(self, request_id, status):
        self.db.execute('''INSERT INTO admin_request_state(request_id,status,updated_at) VALUES(?,?,?)
            ON CONFLICT(request_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at''',
            (request_id, status, now()))

    @staticmethod
    def require_active(row):
        if row['archived_at']:
            raise AdminConflict('Сначала восстановите заявку из архива.')

    def add_note(self, request_id, note, actor):
        with self.transaction():
            self.require_active(self.request(request_id))
            self.event(actor, 'Внутренняя заметка', note, request_id)

    def archive(self, request_id, enabled, actor):
        with self.transaction():
            row = self.request(request_id)
            if bool(row['archived_at']) == enabled:
                return
            self.db.execute('''INSERT INTO admin_request_state(request_id,archived_at) VALUES(?,?)
                ON CONFLICT(request_id) DO UPDATE SET archived_at=excluded.archived_at''',
                (request_id, now() if enabled else None))
            self.event(actor, 'Перенесена в архив' if enabled else 'Восстановлена из архива',
                       request_id=request_id)

    def schedule(self, request_id, day, clock, actor):
        try:
            with self.transaction():
                row = self.request(request_id)
                self.require_active(row)
                if row['status'] in ('done', 'cancelled'):
                    raise AdminConflict('Откройте заявку в работу, чтобы назначить визит.')
                if (row['appointment_day'], row['appointment_time']) == (day, clock):
                    return
                booking = self.store.replace_appointment(request_id, day, clock)
                self.write_status(request_id, 'in_progress')
                old = row['booking']
                self.event(actor, 'Визит перенесён' if old else 'Визит назначен',
                           f'{old} → {booking}' if old else booking, request_id)
        except sqlite3.IntegrityError as error:
            raise SlotUnavailable('Время уже занято. Предыдущий визит сохранён.') from error

    def cancel_visit(self, request_id, actor):
        with self.transaction():
            row = self.request(request_id)
            self.require_active(row)
            if row['status'] == 'done':
                raise AdminConflict('Сначала откройте заявку в работу.')
            if row['booking'] or row['appointment_day']:
                self.store.cancel_appointment(request_id)
                self.write_status(request_id, 'in_progress')
                self.event(actor, 'Визит отменён', row['booking'] or '', request_id)
