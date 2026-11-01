import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from scheduling import TIMES, local_now, validate_slot, slot_start


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical_phone(phone):
    digits = re.sub(r'\D', '', phone)
    # Российские номера 8XXXXXXXXXX и +7XXXXXXXXXX — один контакт.
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    return '+' + digits


class SlotUnavailable(ValueError):
    pass


class SubmissionConflict(ValueError):
    pass


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                phone TEXT, stage TEXT NOT NULL DEFAULT 'phone');
            CREATE TABLE IF NOT EXISTS drafts (user_id INTEGER PRIMARY KEY, problem TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS visits (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, phone TEXT NOT NULL,
                problem TEXT NOT NULL, created TEXT NOT NULL, booking TEXT);
            CREATE TABLE IF NOT EXISTS request_tombstones (id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS booking_selections (user_id INTEGER PRIMARY KEY, day TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL,
                day TEXT NOT NULL, clock TEXT NOT NULL,
                created TEXT NOT NULL, cancelled TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS appointments_slot_unique
                ON appointments(day,clock) WHERE cancelled IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS appointments_request_unique
                ON appointments(request_id) WHERE cancelled IS NULL;
        ''')
        self.db.commit()
        self._migrate_requests()
        self._migrate_sources()

    def _migrate_sources(self):
        # Rebuild once: a website visitor does not have a Telegram user_id.
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(requests)')}
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if 'source' not in columns:
                self.db.execute('''CREATE TABLE requests_with_sources (
                    id INTEGER PRIMARY KEY, user_id INTEGER, phone TEXT NOT NULL,
                    problem TEXT NOT NULL, created TEXT NOT NULL, booking TEXT,
                    source TEXT NOT NULL DEFAULT 'telegram' CHECK(source IN ('telegram','website')),
                    contact_name TEXT NOT NULL DEFAULT '', brand TEXT NOT NULL DEFAULT '',
                    comment TEXT NOT NULL DEFAULT '',
                    CHECK((source='telegram' AND user_id IS NOT NULL)
                        OR (source='website' AND user_id IS NULL)))''')
                self.db.execute('''INSERT INTO requests_with_sources(id,user_id,phone,problem,created,booking)
                    SELECT id,user_id,phone,problem,created,booking FROM requests''')
                self.db.execute('DROP TABLE requests')
                self.db.execute('ALTER TABLE requests_with_sources RENAME TO requests')
                self.db.execute("CREATE UNIQUE INDEX requests_phone_unique ON requests(phone) WHERE source='telegram'")
            columns = {row['name'] for row in self.db.execute('PRAGMA table_info(requests)')}
            if 'preferred_date' not in columns:
                self.db.execute('ALTER TABLE requests ADD COLUMN preferred_date TEXT')
            if 'preferred_time' not in columns:
                self.db.execute('ALTER TABLE requests ADD COLUMN preferred_time TEXT')
            self.db.execute('''CREATE TABLE IF NOT EXISTS web_submissions (
                submission_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
                request_id INTEGER NOT NULL, created TEXT NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS web_notifications (
                request_id INTEGER NOT NULL, admin_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, PRIMARY KEY(request_id,admin_id))''')
            self.db.execute('''CREATE INDEX IF NOT EXISTS web_notifications_due
                ON web_notifications(status,next_attempt)''')

    def _migrate_requests(self):
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='requests_phone_unique'").fetchone():
            return
        # Старые дубли сохраняем в архиве, прежде чем объединить активные заявки.
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS requests_archive (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, phone TEXT NOT NULL,
                problem TEXT NOT NULL, created TEXT NOT NULL, booking TEXT)""")
            groups = {}
            for row in self.db.execute('SELECT * FROM requests ORDER BY id').fetchall():
                groups.setdefault(canonical_phone(row['phone']), []).append(row)
            for phone, rows in groups.items():
                first, last = rows[0], rows[-1]
                if len(rows) > 1:
                    self.db.executemany('INSERT OR IGNORE INTO requests_archive VALUES(?,?,?,?,?,?)',
                                        [tuple(row) for row in rows])
                    self.db.executemany('DELETE FROM requests WHERE id=?', [(row['id'],) for row in rows[1:]])
                booking = next((row['booking'] for row in reversed(rows) if row['booking']), None)
                self.db.execute('UPDATE requests SET phone=?,user_id=?,problem=?,booking=? WHERE id=?',
                                (phone, last['user_id'], last['problem'], booking, first['id']))
            self.db.execute('CREATE UNIQUE INDEX requests_phone_unique ON requests(phone)')

    def visit(self, user):
        timestamp = now()
        with self.db:
            self.db.execute('''INSERT INTO users(id,name,username,first_seen,last_seen)
                VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, username=excluded.username,last_seen=excluded.last_seen''',
                (user.id, user.full_name, user.username, timestamp, timestamp))
            self.db.execute('INSERT INTO visits(user_id,created) VALUES(?,?)', (user.id, timestamp))

    def user(self, uid):
        return self.db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()

    def stage(self, uid, stage):
        with self.db:
            self.db.execute('UPDATE users SET stage=? WHERE id=?', (stage, uid))

    def phone(self, uid, phone):
        with self.db:
            self.db.execute("UPDATE users SET phone=?,stage='problem' WHERE id=?", (canonical_phone(phone), uid))

    def draft(self, uid, problem):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO drafts VALUES(?,?)", (uid, problem))

    def clear_draft(self, uid):
        with self.db:
            self.db.execute("DELETE FROM drafts WHERE user_id=?", (uid,))

    def submit(self, uid):
        draft = self.db.execute("SELECT problem FROM drafts WHERE user_id=?", (uid,)).fetchone()
        problem = draft[0] if draft else "Не указана"
        with self.db:
            row = self.db.execute("""INSERT INTO requests(id,user_id,phone,problem,created)
                VALUES((SELECT COALESCE(MAX(id),0)+1 FROM (
                    SELECT id FROM requests UNION ALL SELECT id FROM request_tombstones)),?,?,?,?)
                ON CONFLICT(phone) WHERE source='telegram' DO UPDATE SET user_id=excluded.user_id,
                    problem=CASE WHEN ? THEN excluded.problem ELSE requests.problem END
                RETURNING id""",
                (uid, canonical_phone(self.user(uid)['phone']), problem, now(), draft is not None)).fetchone()
            self.db.execute("UPDATE users SET stage='booking_date' WHERE id=?", (uid,))
            self.db.execute("DELETE FROM booking_selections WHERE user_id=?", (uid,))
            self.db.execute("DELETE FROM drafts WHERE user_id=?", (uid,))
            return row[0]

    def latest(self, uid):
        user = self.user(uid)
        if not user or not user['phone']:
            return None
        return self.db.execute("SELECT * FROM requests WHERE phone=? AND source='telegram'",
                               (canonical_phone(user['phone']),)).fetchone()

    def web_submission(self, submission_id, payload_hash):
        row = self.db.execute('SELECT * FROM web_submissions WHERE submission_id=?',
                              (submission_id,)).fetchone()
        if row and row['payload_hash'] != payload_hash:
            raise SubmissionConflict('Эта отправка уже принята с другими данными. Обновите страницу.')
        return row['request_id'] if row else None

    def submit_website(self, submission_id, payload_hash, data, admins):
        with self.db:
            existing = self.web_submission(submission_id, payload_hash)
            if existing is not None:
                return existing, False
            timestamp = now()
            row = self.db.execute('''INSERT INTO requests
                (id,user_id,phone,problem,created,source,contact_name,brand,comment,preferred_date,preferred_time)
                VALUES((SELECT COALESCE(MAX(id),0)+1 FROM (
                    SELECT id FROM requests UNION ALL SELECT id FROM request_tombstones)),
                    NULL,?,?,?,'website',?,?,?,?,?) RETURNING id''',
                (data['phone'], data['problem'], timestamp, data['name'], data['brand'], data['comment'],
                 data['preferred_date'],data['preferred_time'])).fetchone()
            request_id = row['id']
            self.db.execute('INSERT INTO web_submissions VALUES(?,?,?,?)',
                            (submission_id, payload_hash, request_id, timestamp))
            self.db.executemany('INSERT INTO web_notifications(request_id,admin_id) VALUES(?,?)',
                                [(request_id, admin_id) for admin_id in sorted(set(admins))])
        return request_id, True

    def due_web_notifications(self, timestamp=None):
        return self.db.execute('''SELECT n.admin_id,n.attempts,r.* FROM web_notifications n
            JOIN requests r ON r.id=n.request_id
            WHERE n.status='pending' AND n.next_attempt<=?
            ORDER BY n.next_attempt,n.request_id,n.admin_id LIMIT 20''',
            (time.time() if timestamp is None else timestamp,)).fetchall()

    def finish_web_notification(self, request_id, admin_id, status='sent'):
        with self.db:
            self.db.execute('UPDATE web_notifications SET status=? WHERE request_id=? AND admin_id=?',
                            (status, request_id, admin_id))

    def retry_web_notification(self, request_id, admin_id, delay):
        with self.db:
            self.db.execute('''UPDATE web_notifications SET attempts=attempts+1,next_attempt=?
                WHERE request_id=? AND admin_id=?''', (time.time()+delay, request_id, admin_id))

    def begin_booking(self, uid):
        with self.db:
            self.db.execute("DELETE FROM booking_selections WHERE user_id=?", (uid,))
            self.db.execute("UPDATE users SET stage='booking_date' WHERE id=?", (uid,))

    def select_day(self, uid, day):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO booking_selections VALUES(?,?)', (uid, day))
            self.db.execute("UPDATE users SET stage='booking_time' WHERE id=?", (uid,))

    def selected_day(self, uid):
        row = self.db.execute('SELECT day FROM booking_selections WHERE user_id=?', (uid,)).fetchone()
        return row['day'] if row else None

    def available_times(self, day, uid):
        request = self.latest(uid)
        request_id = request['id'] if request else -1
        occupied = {row['clock'] for row in self.db.execute(
            'SELECT clock FROM appointments WHERE day=? AND cancelled IS NULL AND request_id<>?',
            (day, request_id))}
        return [clock for clock in TIMES if clock not in occupied and slot_start(day, clock) > local_now()]

    def book(self, uid, day, clock):
        validate_slot(day, clock)
        request = self.latest(uid)
        if not request:
            raise ValueError('Сначала заполните заявку.')
        booking = f'{datetime.fromisoformat(day):%d.%m.%Y} {clock}'
        try:
            with self.db:
                # UNIQUE-индекс не позволит двум клиентам занять один слот.
                # При конфликте транзакция сохраняет прежнюю запись клиента.
                self.db.execute('UPDATE appointments SET cancelled=? WHERE request_id=? AND cancelled IS NULL',
                                (now(), request['id']))
                self.db.execute('INSERT INTO appointments(request_id,day,clock,created) VALUES(?,?,?,?)',
                                (request['id'], day, clock, now()))
                self.db.execute('UPDATE requests SET booking=? WHERE id=?', (booking, request['id']))
                self.db.execute("UPDATE users SET stage='menu' WHERE id=?", (uid,))
                self.db.execute('DELETE FROM booking_selections WHERE user_id=?', (uid,))
        except sqlite3.IntegrityError as error:
            raise SlotUnavailable('Это время уже занято. Выберите другое.') from error
        return booking

    def request(self, request_id):
        return self.db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()

    def delete_request(self, request_id):
        with self.db:
            request = self.request(request_id)
            if not request:
                return False
            # Не переиспользуем ID: старые кнопки не должны затрагивать новые заявки.
            self.db.execute('INSERT OR IGNORE INTO request_tombstones(id) VALUES(?)', (request_id,))
            self.db.execute('UPDATE appointments SET cancelled=? WHERE request_id=? AND cancelled IS NULL',
                            (now(), request_id))
            clients = (self.db.execute('SELECT id FROM users WHERE phone=?', (request['phone'],)).fetchall()
                       if request['source'] == 'telegram' else [])
            for client in clients:
                uid = client['id']
                self.db.execute('DELETE FROM drafts WHERE user_id=?', (uid,))
                self.db.execute('DELETE FROM booking_selections WHERE user_id=?', (uid,))
                self.db.execute("UPDATE users SET stage='menu' WHERE id=?", (uid,))
            self.db.execute('DELETE FROM requests WHERE id=?', (request_id,))
            self.db.execute('DELETE FROM web_notifications WHERE request_id=?', (request_id,))
        return True

    def page(self, kind, page, limit=5):
        queries = {
            'visits': 'SELECT v.created,u.id,u.name,u.username FROM visits v JOIN users u ON u.id=v.user_id ORDER BY v.id DESC',
            'requests': '''SELECT r.*,COALESCE(NULLIF(r.contact_name,''),u.name,'Посетитель сайта') AS name
                FROM requests r LEFT JOIN users u ON u.id=r.user_id ORDER BY r.id DESC''',
        }
        return self.db.execute(queries[kind] + ' LIMIT ? OFFSET ?', (limit + 1, page * limit)).fetchall()

    def counts(self):
        return tuple(self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                     for table in ('users', 'visits', 'requests'))

