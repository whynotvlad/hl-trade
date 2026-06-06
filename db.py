import sqlite3
import os
from typing import Optional
from cryptography.fernet import Fernet

DB_PATH = "users.db"


def _fernet() -> Fernet:
    key = os.getenv("DB_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("DB_ENCRYPTION_KEY not set in .env")
    return Fernet(key.encode())


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id           INTEGER PRIMARY KEY,
                agent_key       TEXT NOT NULL,
                account_address TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                tg_id     INTEGER PRIMARY KEY,
                added_by  INTEGER NOT NULL,
                added_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id     INTEGER NOT NULL,
                coin      TEXT NOT NULL,
                direction TEXT NOT NULL,
                price     REAL NOT NULL
            )
        """)


# ── credentials ───────────────────────────────────────────────────────────────

def register_user(tg_id: int, agent_key: str, account_address: str):
    encrypted = _fernet().encrypt(agent_key.encode()).decode()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (tg_id, agent_key, account_address) VALUES (?, ?, ?)",
            (tg_id, encrypted, account_address),
        )


def get_user(tg_id: int) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT agent_key, account_address FROM users WHERE tg_id = ?", (tg_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "agent_key": _fernet().decrypt(row[0].encode()).decode(),
        "account_address": row[1],
    }


def is_registered(tg_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT 1 FROM users WHERE tg_id = ?", (tg_id,)
        ).fetchone() is not None


def get_all_registered_users() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT tg_id FROM users").fetchall()
    return [{"tg_id": r[0]} for r in rows]


# ── access control ────────────────────────────────────────────────────────────

def add_allowed_user(tg_id: int, added_by: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO allowed_users (tg_id, added_by, added_at) VALUES (?, ?, datetime('now'))",
            (tg_id, added_by),
        )


def remove_allowed_user(tg_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM allowed_users WHERE tg_id = ?", (tg_id,))
        conn.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))
        conn.execute("DELETE FROM alerts WHERE tg_id = ?", (tg_id,))


def is_allowed_user(tg_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT 1 FROM allowed_users WHERE tg_id = ?", (tg_id,)
        ).fetchone() is not None


def get_allowed_users() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT tg_id, added_by, added_at FROM allowed_users ORDER BY added_at"
        ).fetchall()
    return [{"tg_id": r[0], "added_by": r[1], "added_at": r[2]} for r in rows]


# ── price alerts ──────────────────────────────────────────────────────────────

def add_alert(tg_id: int, coin: str, direction: str, price: float) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO alerts (tg_id, coin, direction, price) VALUES (?, ?, ?, ?)",
            (tg_id, coin, direction, price),
        )
        return cur.lastrowid


def get_alerts(tg_id: int) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, coin, direction, price FROM alerts WHERE tg_id = ? ORDER BY id",
            (tg_id,),
        ).fetchall()
    return [{"id": r[0], "coin": r[1], "direction": r[2], "price": r[3]} for r in rows]


def get_all_alerts() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, tg_id, coin, direction, price FROM alerts"
        ).fetchall()
    return [{"id": r[0], "tg_id": r[1], "coin": r[2], "direction": r[3], "price": r[4]} for r in rows]


def delete_alert(alert_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
