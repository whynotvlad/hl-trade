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
            "SELECT agent_key, account_address FROM users WHERE tg_id = ?",
            (tg_id,),
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
