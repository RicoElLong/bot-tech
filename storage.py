import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical_phone(phone):
    digits = re.sub(r'\D', '', phone)
    # Российские номера 8XXXXXXXXXX и +7XXXXXXXXXX — один контакт.
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    return '+' + digits


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
        ''')
        self.db.commit()
        self._migrate_requests()

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
            row = self.db.execute("""INSERT INTO requests(user_id,phone,problem,created) VALUES(?,?,?,?)
                ON CONFLICT(phone) DO UPDATE SET user_id=excluded.user_id,
                    problem=CASE WHEN ? THEN excluded.problem ELSE requests.problem END
                RETURNING id""",
                (uid, canonical_phone(self.user(uid)['phone']), problem, now(), draft is not None)).fetchone()
            self.db.execute("UPDATE users SET stage='menu' WHERE id=?", (uid,))
            self.db.execute("DELETE FROM drafts WHERE user_id=?", (uid,))
            return row[0]

    def latest(self, uid):
        user = self.user(uid)
        if not user or not user['phone']:
            return None
        return self.db.execute('SELECT * FROM requests WHERE phone=?', (canonical_phone(user['phone']),)).fetchone()

    def book(self, uid, text):
        with self.db:
            self.db.execute('UPDATE requests SET booking=? WHERE id=?', (text, self.latest(uid)['id']))
            self.db.execute("UPDATE users SET stage='menu' WHERE id=?", (uid,))

    def page(self, kind, page, limit=5):
        queries = {
            'visits': 'SELECT v.created,u.id,u.name,u.username FROM visits v JOIN users u ON u.id=v.user_id ORDER BY v.id DESC',
            'requests': 'SELECT r.*,u.name FROM requests r JOIN users u ON u.id=r.user_id ORDER BY r.id DESC',
        }
        return self.db.execute(queries[kind] + ' LIMIT ? OFFSET ?', (limit + 1, page * limit)).fetchall()

    def counts(self):
        return tuple(self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                     for table in ('users', 'visits', 'requests'))
