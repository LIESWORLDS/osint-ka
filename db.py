"""
SQLite storage layer.

Tables:
  users          - wallet / referral / spin data
  settings       - key/value bot settings (maintenance mode, banner image)
  admins         - dynamic list of admin user IDs
  force_channels - dynamically managed list of channels users must join
"""

import sqlite3
import time
from contextlib import contextmanager

DB_PATH = "bot.db"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                credits INTEGER DEFAULT 0,
                referred_by INTEGER,
                is_verified INTEGER DEFAULT 0,
                joined_at INTEGER,
                last_spin INTEGER DEFAULT 0,
                spin_streak INTEGER DEFAULT 0
            )
            """
        )
        # Safe migration for databases created before spin_streak existed.
        try:
            conn.execute("ALTER TABLE users ADD COLUMN spin_streak INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS force_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT UNIQUE,
                invite_link TEXT,
                title TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS redeem_codes (
                code TEXT PRIMARY KEY,
                credits INTEGER NOT NULL,
                max_uses INTEGER NOT NULL DEFAULT 1,
                used_count INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER,
                created_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS redeem_uses (
                code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                used_at INTEGER,
                PRIMARY KEY (code, user_id)
            )
            """
        )
        conn.commit()


# ---------------------------------------------------------------- users ----
def get_user(user_id: int):
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return cur.fetchone()


def create_user(user_id: int, name: str, referred_by: int = None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (user_id, name, credits, referred_by, is_verified, joined_at) "
            "VALUES (?, ?, 0, ?, 1, ?)",
            (user_id, name, referred_by, int(time.time())),
        )
        conn.commit()


def update_credits(user_id: int, delta: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET credits = credits + ? WHERE user_id = ?", (delta, user_id)
        )
        conn.commit()


def set_verified(user_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_verified = 1 WHERE user_id = ?", (user_id,))
        conn.commit()


def get_referral_count(user_id: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) as c FROM users WHERE referred_by = ?", (user_id,)
        )
        return cur.fetchone()["c"]


def record_spin(user_id: int, ts: int, streak: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET last_spin = ?, spin_streak = ? WHERE user_id = ?",
            (ts, streak, user_id),
        )
        conn.commit()


def count_users() -> int:
    with get_conn() as conn:
        cur = conn.execute("SELECT COUNT(*) as c FROM users")
        return cur.fetchone()["c"]


def top_by_credits(limit: int = 10):
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM users ORDER BY credits DESC, joined_at ASC LIMIT ?", (limit,)
        )
        return cur.fetchall()


def get_rank(user_id: int) -> int:
    """1-based rank of this user by credits (highest credits = rank 1)."""
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) + 1 as rank FROM users WHERE credits > "
            "(SELECT credits FROM users WHERE user_id = ?)",
            (user_id,),
        )
        return cur.fetchone()["rank"]


def list_users(offset: int = 0, limit: int = 10):
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?", (limit, offset)
        )
        return cur.fetchall()


# ------------------------------------------------------------- settings ----
def get_setting(key: str, default=None):
    with get_conn() as conn:
        cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


# --------------------------------------------------------------- admins ----
def is_admin(user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
        return cur.fetchone() is not None


def add_admin(user_id: int):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
        conn.commit()


def list_admins():
    with get_conn() as conn:
        cur = conn.execute("SELECT user_id FROM admins")
        return [r["user_id"] for r in cur.fetchall()]


# --------------------------------------------------------- force channels --
def add_force_channel(chat_id: str, invite_link: str, title: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO force_channels (chat_id, invite_link, title) "
            "VALUES (?, ?, ?)",
            (chat_id, invite_link, title),
        )
        conn.commit()


def remove_force_channel(channel_row_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM force_channels WHERE id = ?", (channel_row_id,))
        conn.commit()


def list_force_channels():
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM force_channels ORDER BY id")
        return cur.fetchall()


# ----------------------------------------------------------- redeem codes --
def create_redeem_code(code: str, credits: int, max_uses: int, created_by: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO redeem_codes (code, credits, max_uses, used_count, created_by, created_at) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            (code, credits, max_uses, created_by, int(time.time())),
        )
        conn.commit()


def get_redeem_code(code: str):
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM redeem_codes WHERE code = ?", (code,))
        return cur.fetchone()


def has_redeemed(code: str, user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT 1 FROM redeem_uses WHERE code = ? AND user_id = ?", (code, user_id)
        )
        return cur.fetchone() is not None


def redeem_code_for_user(code: str, user_id: int, ts: int):
    """Records this user's redemption and bumps the code's used_count."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO redeem_uses (code, user_id, used_at) VALUES (?, ?, ?)",
            (code, user_id, ts),
        )
        conn.execute("UPDATE redeem_codes SET used_count = used_count + 1 WHERE code = ?", (code,))
        conn.commit()


def list_redeem_codes():
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM redeem_codes ORDER BY created_at DESC")
        return cur.fetchall()


def delete_redeem_code(code: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM redeem_codes WHERE code = ?", (code,))
        conn.execute("DELETE FROM redeem_uses WHERE code = ?", (code,))
        conn.commit()
