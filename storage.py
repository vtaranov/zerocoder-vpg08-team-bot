"""Chronological journal, durable sessions and a retryable indexing outbox."""
import sqlite3
from uuid import uuid4
from pathlib import Path
from contextlib import contextmanager, closing

class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('active','closed')),
                summary TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS active_session ON sessions(scope) WHERE state='active';
            CREATE TABLE IF NOT EXISTS messages (
                scope TEXT NOT NULL, message_id INTEGER NOT NULL,
                author_id TEXT NOT NULL, author TEXT NOT NULL, created_at TEXT NOT NULL,
                text TEXT NOT NULL, session_id TEXT, indexed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(scope,message_id));
            CREATE INDEX IF NOT EXISTS session_messages ON messages(session_id,message_id);
            """)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=20)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def begin(self, scope):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM sessions WHERE scope=? AND state='active'", (scope,)).fetchone()
            if row:
                return row['id'], False
            sid = uuid4().hex
            db.execute("INSERT INTO sessions VALUES (?,?,'active',NULL)", (sid, scope))
            return sid, True

    def add(self, scope, message_id, author_id, author, created_at, text):
        if not text.strip() or len(text) > 8000:
            raise ValueError("Сообщение должно содержать от 1 до 8000 символов.")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT id FROM sessions WHERE scope=? AND state='active'", (scope,)).fetchone()
            db.execute("""INSERT OR IGNORE INTO messages
                (scope,message_id,author_id,author,created_at,text,session_id) VALUES (?,?,?,?,?,?,?)""",
                (scope, message_id, str(author_id), author, created_at, text, active['id'] if active else None))
            return dict(db.execute("SELECT * FROM messages WHERE scope=? AND message_id=?", (scope, message_id)).fetchone())

    def pending(self, scope, limit=100):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM messages WHERE scope=? AND indexed=0 ORDER BY message_id LIMIT ?", (scope, limit))]

    def mark_indexed(self, scope, mids):
        with self.db() as db:
            db.executemany("UPDATE messages SET indexed=1 WHERE scope=? AND message_id=?", [(scope, m) for m in mids])

    def recent(self, scope, limit=30, exclude=None):
        with self.db() as db:
            rows = db.execute("""SELECT * FROM messages WHERE scope=? AND message_id!=?
                ORDER BY message_id DESC LIMIT ?""", (scope, exclude if exclude is not None else -1, limit)).fetchall()
            return [dict(r) for r in reversed(rows)]

    def close_session(self, scope):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sessions WHERE scope=? AND state='active'", (scope,)).fetchone()
            if row:
                db.execute("UPDATE sessions SET state='closed' WHERE id=?", (row['id'],))
            else:
                row = db.execute("SELECT * FROM sessions WHERE scope=? AND state='closed' ORDER BY rowid DESC LIMIT 1", (scope,)).fetchone()
            return dict(row) if row else None

    def session_rows(self, scope, sid):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM messages WHERE scope=? AND session_id=? ORDER BY message_id", (scope, sid))]

    def save_summary(self, sid, summary):
        with self.db() as db:
            db.execute("UPDATE sessions SET summary=? WHERE id=?", (summary, sid))

    def status(self, scope):
        with self.db() as db:
            n = db.execute("SELECT COUNT(*),COALESCE(SUM(indexed=0),0) FROM messages WHERE scope=?", (scope,)).fetchone()
            active = db.execute("SELECT id FROM sessions WHERE scope=? AND state='active'", (scope,)).fetchone()
            return {'messages': n[0], 'pending': n[1], 'listening': bool(active)}
