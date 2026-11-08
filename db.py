"""Sklad, sotuvchilar, xodimlar va bot holati uchun SQLite baza.

Fayl Railway volume'ida turishi kerak (DB_FAYL=/data/hisobot.db), aks holda har deployda o'chadi.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("VAQT_ZONASI", "Asia/Tashkent"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,              -- Cliniccards product_id
    name TEXT NOT NULL,
    category TEXT DEFAULT '',
    unit TEXT DEFAULT '',
    qty REAL NOT NULL DEFAULT 0,      -- joriy qoldiq (movements yig'indisi)
    min_qty REAL NOT NULL DEFAULT 0,  -- shundan kam bo'lsa buyurtma taklif qilinadi
    order_qty REAL NOT NULL DEFAULT 0,
    supplier_id INTEGER REFERENCES suppliers(id),
    last_price REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT DEFAULT '',
    cc_supplier_id TEXT,
    chat_id INTEGER,
    token TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    is_taker INTEGER NOT NULL DEFAULT 0,    -- har kuni "skladdan narsa oldingizmi?" so'raladi
    is_checker INTEGER NOT NULL DEFAULT 0,  -- skladni tekshiradi
    is_admin INTEGER NOT NULL DEFAULT 0,    -- tasdiq va buyurtma takliflari keladi
    chat_id INTEGER,
    token TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id),
    delta REAL NOT NULL,
    kind TEXT NOT NULL,     -- boshlangich | kirim | chiqim | tuzatish
    ref TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_invoices (invoice_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES people(id),
    day TEXT NOT NULL,
    status TEXT NOT NULL,   -- soraldi | olinmadi | kiritilmoqda | tekshiruvda | tasdiqlandi | farq | cc_chiqarildi
    created_at TEXT NOT NULL,
    checked_by INTEGER REFERENCES people(id),
    checked_at TEXT,
    cc_done_at TEXT
);
CREATE TABLE IF NOT EXISTS report_items (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES reports(id),
    product_id TEXT NOT NULL REFERENCES products(id),
    qty REAL NOT NULL,
    expected_after REAL,
    actual_after REAL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id),
    supplier_id INTEGER REFERENCES suppliers(id),
    qty REAL NOT NULL,
    price REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,   -- taklif | yuborildi | qabul | bekor
    created_at TEXT NOT NULL,
    sent_at TEXT,
    answered_at TEXT
);
CREATE TABLE IF NOT EXISTS bot_state (chat_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def new_token() -> str:
    return secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    # --- umumiy ----------------------------------------------------------
    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def run(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    def tx(self):
        """`with db.tx():` — bir nechta o'zgarishni bitta tranzaksiyada bajarish."""
        db = self

        class _Tx:
            def __enter__(self):
                db.lock.acquire()
                db.conn.execute("BEGIN")

            def __exit__(self, exc_type, *_):
                try:
                    db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    db.lock.release()
        return _Tx()

    def meta(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.run("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))

    # --- bot holati -------------------------------------------------------
    def state(self, chat_id: int) -> dict:
        row = self.one("SELECT data FROM bot_state WHERE chat_id=?", (chat_id,))
        return json.loads(row["data"]) if row else {}

    def set_state(self, chat_id: int, data: dict | None) -> None:
        if not data:
            self.run("DELETE FROM bot_state WHERE chat_id=?", (chat_id,))
        else:
            self.run("INSERT INTO bot_state(chat_id, data) VALUES(?, ?) "
                     "ON CONFLICT(chat_id) DO UPDATE SET data=excluded.data", (chat_id, json.dumps(data)))

    # --- qoldiq -----------------------------------------------------------
    def move(self, product_id: str, delta: float, kind: str, ref: str = "", note: str = "") -> None:
        """Harakat yozadi va qoldiqni yangilaydi (chaqiruvchi tranzaksiya ichida bo'lishi mumkin)."""
        with self.lock:
            self.conn.execute("INSERT INTO movements(product_id, delta, kind, ref, note, created_at) VALUES(?,?,?,?,?,?)",
                              (product_id, delta, kind, ref, note, now()))
            self.conn.execute("UPDATE products SET qty = qty + ? WHERE id=?", (delta, product_id))

    def set_qty(self, product_id: str, actual: float, ref: str = "", note: str = "") -> float:
        """Qoldiqni haqiqiy songa tenglashtiradi (inventarizatsiya). Farqni qaytaradi."""
        row = self.one("SELECT qty FROM products WHERE id=?", (product_id,))
        diff = actual - (row["qty"] if row else 0)
        if diff:
            self.move(product_id, diff, "tuzatish", ref, note)
        return diff
