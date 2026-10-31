import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


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
            self.db.execute("UPDATE users SET phone=?,stage='problem' WHERE id=?", (phone, uid))

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
            row = self.db.execute('INSERT INTO requests(user_id,phone,problem,created) VALUES(?,?,?,?)',
                                  (uid, self.user(uid)['phone'], problem, now()))
            self.db.execute("UPDATE users SET stage='menu' WHERE id=?", (uid,))
            self.db.execute("DELETE FROM drafts WHERE user_id=?", (uid,))
            return row.lastrowid

    def latest(self, uid):
        return self.db.execute('SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 1', (uid,)).fetchone()

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
