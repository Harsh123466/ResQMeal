from __future__ import annotations

import os
import re
import secrets
import sqlite3
import atexit
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .security import hash_password

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "annsetu.db"
MAX_COLLECTION_RADIUS_KM = 35

def load_local_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

load_local_env()
DATABASE_URL = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL", "").strip()

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # SQLite-only development remains available without psycopg installed.
    psycopg = None
    dict_row = None

try:
    from psycopg_pool import ConnectionPool
except ImportError:  # Fall back to direct connections until the optional pool extra is installed.
    ConnectionPool = None

_postgres_pool = None
_postgres_pool_lock = threading.Lock()

class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


class CompatRow(dict):
    """Mapping row that also supports SQLite-style positional access."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class CursorAdapter:
    def __init__(self, cursor):
        self.cursor = cursor

    @staticmethod
    def adapt(row):
        return CompatRow(row) if row is not None and not isinstance(row, CompatRow) else row

    def fetchone(self):
        return self.adapt(self.cursor.fetchone())

    def fetchall(self):
        return [self.adapt(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        for row in self.cursor:
            yield self.adapt(row)


class PostgresConnection:
    is_postgres = True

    def __init__(self, connection, pool=None):
        self.connection = connection
        self.pool = pool

    def execute(self, sql: str, params=()):
        cursor = self.connection.cursor()
        cursor.execute(sql.replace("?", "%s"), params)
        return CursorAdapter(cursor)

    def executescript(self, script: str):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            if self.pool is not None:
                self.pool.putconn(self.connection)
            else:
                self.connection.close()


def postgres_url() -> str:
    url = DATABASE_URL
    # This app opens short-lived connections per request. Supabase's shared
    # transaction pooler is intended for this pattern (6543); 5432 is session
    # mode and can exhaust/lose sessions under concurrent dashboard refreshes.
    url = re.sub(r"(\.pooler\.supabase\.com):5432(?=/)", r"\1:6543", url)
    if url and "sslmode=" not in url:
        url += "&sslmode=require" if "?" in url else "?sslmode=require"
    return url


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime | None = None) -> str:
    return (value or now()).isoformat()


def connect():
    if DATABASE_URL:
        if psycopg is None:
            raise RuntimeError("Supabase database configured but psycopg is not installed. Install backend requirements.")
        global _postgres_pool
        if ConnectionPool is None:
            return PostgresConnection(psycopg.connect(postgres_url(), row_factory=dict_row, connect_timeout=10, prepare_threshold=None))
        if _postgres_pool is None:
            with _postgres_pool_lock:
                if _postgres_pool is None:
                    _postgres_pool = ConnectionPool(
                        conninfo=postgres_url(), min_size=1, max_size=5, timeout=10,
                        kwargs={"row_factory": dict_row, "connect_timeout": 10, "prepare_threshold": None},
                        open=False,
                    )
                    _postgres_pool.open()
                    atexit.register(_postgres_pool.close)
        return PostgresConnection(_postgres_pool.getconn(), _postgres_pool)
    db = sqlite3.connect(DB_PATH, timeout=15, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db() -> None:
    with connect() as db:
        if not getattr(db, "is_postgres", False):
            db.execute("PRAGMA foreign_keys = OFF")
        schema = """
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('donor','recipient','admin','volunteer')),
          password_hash TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recipients (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, contact TEXT NOT NULL,
          address TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
          capacity_kg REAL NOT NULL, categories TEXT NOT NULL,
          storage TEXT NOT NULL, hours TEXT NOT NULL,
          verification_status TEXT NOT NULL DEFAULT 'pending',
          evidence TEXT NOT NULL DEFAULT '', reference_id TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, user_id TEXT, verification_report TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS donations (
          id TEXT PRIMARY KEY, donor_name TEXT NOT NULL, food_name TEXT NOT NULL,
          category TEXT NOT NULL, quantity_kg REAL NOT NULL, prepared_at TEXT NOT NULL,
          expires_at TEXT NOT NULL, storage TEXT NOT NULL, temperature_c REAL,
          pickup_address TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
          notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
          created_at TEXT NOT NULL, donor_id TEXT, delivery_code TEXT
        );
        CREATE TABLE IF NOT EXISTS portions (
          id TEXT PRIMARY KEY, donation_id TEXT NOT NULL REFERENCES donations(id),
          recipient_id TEXT NOT NULL REFERENCES recipients(id), quantity_kg REAL NOT NULL,
          status TEXT NOT NULL DEFAULT 'offered', otp TEXT NOT NULL,
          pickup_at TEXT, received_at TEXT, created_at TEXT NOT NULL,
          pickup_confirmed_by TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, donation_id TEXT NOT NULL,
          portion_id TEXT, event_type TEXT NOT NULL, detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS documents (
          id TEXT PRIMARY KEY, recipient_id TEXT NOT NULL REFERENCES recipients(id),
          owner_id TEXT NOT NULL REFERENCES users(id), filename TEXT NOT NULL,
          content_type TEXT NOT NULL, encrypted_data BLOB NOT NULL, sha256 TEXT NOT NULL,
          extracted_text TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
          kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
          read_at TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_donations_status ON donations(status);
        CREATE INDEX IF NOT EXISTS idx_donations_donor_created ON donations(donor_id,created_at);
        CREATE INDEX IF NOT EXISTS idx_portions_donation ON portions(donation_id);
        CREATE INDEX IF NOT EXISTS idx_portions_donation_status ON portions(donation_id,status);
        CREATE INDEX IF NOT EXISTS idx_portions_recipient_status ON portions(recipient_id,status);
        CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id,created_at);
        """
        if getattr(db, "is_postgres", False):
            schema = schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
            schema = schema.replace(" REAL ", " DOUBLE PRECISION ").replace(" BLOB ", " BYTEA ")
        db.executescript(schema)
        if getattr(db, "is_postgres", False):
            db.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check")
            db.execute("ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('donor','recipient','admin','volunteer'))")
        else:
            role_schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()["sql"]
            if "'volunteer'" not in role_schema:
                db.execute("CREATE TABLE users_new (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('donor','recipient','admin','volunteer')), password_hash TEXT NOT NULL, created_at TEXT NOT NULL)")
                db.execute("INSERT INTO users_new SELECT id,email,name,role,password_hash,created_at FROM users")
                db.execute("DROP TABLE users")
                db.execute("ALTER TABLE users_new RENAME TO users")
                db.commit()
                db.execute("PRAGMA foreign_keys = ON")
        for table, column, declaration in (
            ("recipients", "user_id", "TEXT"),
            ("recipients", "verification_report", "TEXT NOT NULL DEFAULT '{}'"),
            ("donations", "donor_id", "TEXT"),
            ("donations", "delivery_code", "TEXT"),
            ("portions", "pickup_confirmed_by", "TEXT"),
        ):
            if getattr(db, "is_postgres", False):
                declaration = declaration.replace("TEXT", "TEXT")
                db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {declaration}")
            else:
                columns = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_donations_delivery_code ON donations(delivery_code)")
        for row in db.execute("SELECT id FROM donations WHERE delivery_code IS NULL OR delivery_code='' ").fetchall():
            while True:
                code = secrets.token_hex(8).upper()
                if not db.execute("SELECT 1 FROM donations WHERE delivery_code=?", (code,)).fetchone():
                    db.execute("UPDATE donations SET delivery_code=? WHERE id=?", (code, row[0]))
                    break
        if not getattr(db, "is_postgres", False):
            db.commit()
            db.execute("PRAGMA foreign_keys = ON")
        admin_email = os.getenv("ANNSETU_ADMIN_EMAIL", "").strip().lower()
        admin_password = os.getenv("ANNSETU_ADMIN_PASSWORD", "")
        if admin_email and admin_password and not db.execute("SELECT 1 FROM users WHERE email=?", (admin_email,)).fetchone():
            db.execute("INSERT INTO users (id,email,name,role,password_hash,created_at) VALUES (?,?,?,?,?,?)",
                       (str(uuid4()), admin_email, "AnnSetu Administrator", "admin", hash_password(admin_password), stamp()))
