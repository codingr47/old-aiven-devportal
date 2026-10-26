"""Transactional checkpoints. One writer lock covers generation and review changes."""

import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time TEXT, kind TEXT, data TEXT)")

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def load(self):
        with self.connect() as db:
            row = db.execute("SELECT data FROM checkpoint WHERE id=1").fetchone()
        if row is None:
            raise ValueError("No inventory exists. Run inventory first.")
        return json.loads(row[0])

    def initialize(self, state):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM checkpoint").fetchone():
                raise ValueError("Database already has a snapshot; use a new database for a new run.")
            db.execute("INSERT INTO checkpoint VALUES (1, ?)", (json.dumps(state),))

    @contextlib.contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM checkpoint WHERE id=1").fetchone()
            if not row:
                raise ValueError("No inventory exists")
            state = json.loads(row[0])
            yield state
            db.execute("UPDATE checkpoint SET data=? WHERE id=1", (json.dumps(state),))

    def event(self, kind, data):
        with self.connect() as db:
            db.execute("INSERT INTO events(time,kind,data) VALUES (?,?,?)", (now(), kind, json.dumps(data)))

    def save(self, state):
        with self.connect() as db:
            db.execute("UPDATE checkpoint SET data=? WHERE id=1", (json.dumps(state),))

    @contextlib.contextmanager
    def writer(self):
        """OS lock is released even when the process crashes; readers remain available."""
        lock = open(str(self.path) + ".lock", "a+b")
        lock.seek(0)
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise ValueError("Another generation or review write is active; try again when it finishes.") from None
        try:
            yield
        finally:
            lock.close()
