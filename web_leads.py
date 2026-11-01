"""Storage for website callbacks. Keep the database on the Russian server."""
import hashlib
import hmac
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
from uuid import uuid4

CONSENT_VERSION = '2026-11-01-v1'
BRANDS = ('Bosch', 'LG', 'Samsung', 'Indesit', 'Ariston', 'Beko', 'Candy',
          'Electrolux', 'Haier', 'Siemens', 'Whirlpool', 'Atlant', 'Zanussi', 'Другая / не знаю')
PROBLEMS = ('Не сливает воду', 'Не набирает воду', 'Не отжимает', 'Не крутится барабан',
            'Не включается', 'Не греет воду', 'Протекает', 'Шумит / вибрирует',
            'Не открывается дверца', 'Показывает ошибку', 'Выбивает автомат', 'Другое / не знаю')


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class LeadStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS website_leads (
                    id TEXT PRIMARY KEY, phone TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, brand TEXT NOT NULL, problem TEXT NOT NULL,
                    comment TEXT NOT NULL, consent_at TEXT NOT NULL,
                    consent_version TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS website_submissions (
                    id TEXT PRIMARY KEY, lead_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS website_outbox (
                    id INTEGER PRIMARY KEY, lead_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    sent_at TEXT, attempts INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS website_deliveries (
                    outbox_id INTEGER NOT NULL, admin_id TEXT NOT NULL, delivered_at TEXT NOT NULL,
                    PRIMARY KEY(outbox_id,admin_id));
                CREATE TABLE IF NOT EXISTS website_rate (
                    client_hash TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL);
            ''')
        Path(path).chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def consume_rate(self, client_hash, timestamp):
        window = timestamp // 600
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM website_rate WHERE window < ?', (window,))
            row = db.execute('SELECT count FROM website_rate WHERE client_hash=? AND window=?',
                             (client_hash, window)).fetchone()
            if row and row['count'] >= 5:
                return False
            db.execute('''INSERT INTO website_rate VALUES(?,?,1)
                ON CONFLICT(client_hash) DO UPDATE SET count=website_rate.count+1''', (client_hash, window))
            return True

    def save(self, submission_id, payload, payload_hash, consent_version):
        timestamp = utcnow()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM website_submissions WHERE id=?', (submission_id,)).fetchone()
            if existing:
                if not hmac.compare_digest(existing['payload_hash'], payload_hash):
                    raise ValueError('Заявка с этим идентификатором уже содержит другие данные.')
                return existing['lead_id']
            previous = db.execute('SELECT id FROM website_leads WHERE phone=?', (payload['phone'],)).fetchone()
            lead_id = previous['id'] if previous else str(uuid4())
            db.execute('''INSERT INTO website_leads VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(phone) DO UPDATE SET name=excluded.name,brand=excluded.brand,
                problem=excluded.problem,comment=excluded.comment,consent_at=excluded.consent_at,
                consent_version=excluded.consent_version,updated_at=excluded.updated_at''',
                (lead_id, payload['phone'], payload['name'], payload['brand'], payload['problem'],
                 payload['comment'], timestamp, consent_version, timestamp, timestamp))
            db.execute('INSERT INTO website_submissions VALUES(?,?,?,?)',
                       (submission_id, lead_id, payload_hash, timestamp))
            db.execute('INSERT INTO website_outbox(lead_id,created_at) VALUES(?,?)', (lead_id, timestamp))
        return lead_id

    def get(self, lead_id):
        with self.connect() as db:
            return db.execute('SELECT * FROM website_leads WHERE id=?', (lead_id,)).fetchone()

    def pending(self):
        with self.connect() as db:
            return db.execute('SELECT * FROM website_outbox WHERE sent_at IS NULL ORDER BY id LIMIT 20').fetchall()

    def delivered_to(self, item_id, admin_id):
        with self.connect() as db:
            return db.execute('SELECT 1 FROM website_deliveries WHERE outbox_id=? AND admin_id=?',
                              (item_id, admin_id)).fetchone() is not None

    def mark_delivered(self, item_id, admin_id):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO website_deliveries VALUES(?,?,?)', (item_id, admin_id, utcnow()))

    def mark_attempt(self, item_id, success):
        with self.connect() as db:
            db.execute('UPDATE website_outbox SET attempts=attempts+1,sent_at=? WHERE id=?',
                       (utcnow() if success else None, item_id))

    def delete(self, lead_id):
        with self.connect() as db:
            db.execute('DELETE FROM website_deliveries WHERE outbox_id IN (SELECT id FROM website_outbox WHERE lead_id=?)', (lead_id,))
            db.execute('DELETE FROM website_outbox WHERE lead_id=?', (lead_id,))
            db.execute('DELETE FROM website_submissions WHERE lead_id=?', (lead_id,))
            return db.execute('DELETE FROM website_leads WHERE id=?', (lead_id,)).rowcount > 0

    def purge(self, days=30):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self.connect() as db:
            db.execute('DELETE FROM website_deliveries WHERE outbox_id IN (SELECT o.id FROM website_outbox o JOIN website_leads l ON l.id=o.lead_id WHERE l.updated_at < ?)', (cutoff,))
            db.execute('DELETE FROM website_outbox WHERE lead_id IN (SELECT id FROM website_leads WHERE updated_at < ?)', (cutoff,))
            db.execute('DELETE FROM website_submissions WHERE lead_id IN (SELECT id FROM website_leads WHERE updated_at < ?)', (cutoff,))
            db.execute('DELETE FROM website_leads WHERE updated_at < ?', (cutoff,))


def client_hash(client, secret):
    return hmac.new(secret, client.encode(), hashlib.sha256).hexdigest()
