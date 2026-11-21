"""SQLite persistence with connection-per-operation semantics (PostgreSQL-ready SQL)."""

import sqlite3
from contextlib import contextmanager

from .config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  google_sub TEXT,
  display_name TEXT,
  age INTEGER,
  accepted_terms_at TEXT,
  accepted_privacy_at TEXT,
  accepted_youth_at TEXT,
  policy_version TEXT,
  role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS oauth_registrations (
  nonce_hash TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  google_sub TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '',
  existing_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL DEFAULT '새 대화',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS conversations_user_updated ON conversations(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN ('user','assistant')),
  content TEXT NOT NULL,
  feedback INTEGER CHECK(feedback IN (-1,1)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'general',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS memories_user ON memories(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS datasets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'general',
  source TEXT NOT NULL DEFAULT 'text',
  content_hash TEXT NOT NULL,
  consent INTEGER NOT NULL CHECK(consent IN (0,1)),
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  reviewed_by INTEGER REFERENCES users(id),
  reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS datasets_status ON datasets(status, created_at DESC);
CREATE INDEX IF NOT EXISTS datasets_hash ON datasets(content_hash);
CREATE TABLE IF NOT EXISTS training_jobs (
  id TEXT PRIMARY KEY,
  owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  progress REAL NOT NULL DEFAULT 0,
  epoch INTEGER NOT NULL DEFAULT 0,
  total_epochs INTEGER NOT NULL,
  training_loss REAL,
  validation_loss REAL,
  baseline_loss REAL,
  validation_accuracy REAL,
  example_count INTEGER NOT NULL DEFAULT 0,
  dataset_ids TEXT NOT NULL,
  checkpoint_path TEXT,
  candidate_path TEXT,
  pause_requested INTEGER NOT NULL DEFAULT 0,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  worker_pid INTEGER,
  error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_recent ON training_jobs(created_at DESC);
CREATE TABLE IF NOT EXISTS training_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES training_jobs(id) ON DELETE CASCADE,
  epoch INTEGER NOT NULL,
  training_loss REAL NOT NULL,
  validation_loss REAL NOT NULL,
  validation_accuracy REAL NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(job_id, epoch)
);
CREATE TABLE IF NOT EXISTS model_versions (
  version TEXT PRIMARY KEY,
  checkpoint_path TEXT NOT NULL,
  validation_loss REAL NOT NULL,
  training_job_id TEXT REFERENCES training_jobs(id) ON DELETE SET NULL,
  active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class ClosingConnection(sqlite3.Connection):
    """sqlite3's context manager commits but does not close, so close at scope exit."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


@contextmanager
def transaction():
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(training_jobs)")}
        if "worker_pid" not in columns:
            connection.execute("ALTER TABLE training_jobs ADD COLUMN worker_pid INTEGER")
        user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
        migrations = {
            "google_sub": "TEXT",
            "display_name": "TEXT",
            "age": "INTEGER",
            "accepted_terms_at": "TEXT",
            "accepted_privacy_at": "TEXT",
            "accepted_youth_at": "TEXT",
            "policy_version": "TEXT",
        }
        for column, column_type in migrations.items():
            if column not in user_columns:
                connection.execute(f"ALTER TABLE users ADD COLUMN {column} {column_type}")
        oauth_columns = {row["name"] for row in connection.execute("PRAGMA table_info(oauth_registrations)")}
        if "existing_user_id" not in oauth_columns:
            connection.execute("ALTER TABLE oauth_registrations ADD COLUMN existing_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_google_sub ON users(google_sub) WHERE google_sub IS NOT NULL")
        connection.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('auto_training_enabled','1')")
        connection.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('auto_training_min_items','1')")


def as_dict(row):
    return dict(row) if row is not None else None
