"""Хранилище SQLite: боты, их состояние (открытая позиция) и история сделок."""
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from . import config

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def init() -> None:
    global _conn
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                mode TEXT NOT NULL,
                params TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER NOT NULL,
                ts INTEGER NOT NULL,
                action TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                amount REAL NOT NULL,
                fee REAL NOT NULL,
                pnl REAL,
                reason TEXT
            );
            CREATE INDEX IF NOT EXISTS trades_bot ON trades(bot_id, ts);
        """)
        _conn.commit()


def _row(r: sqlite3.Row) -> Dict[str, Any]:
    d = dict(r)
    d["params"] = json.loads(d["params"])
    d["state"] = json.loads(d["state"])
    d["enabled"] = bool(d["enabled"])
    return d


def list_bots() -> List[Dict[str, Any]]:
    with _lock:
        rows = _conn.execute("SELECT * FROM bots ORDER BY id").fetchall()
    return [_row(r) for r in rows]


def get_bot(bot_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        r = _conn.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
    return _row(r) if r else None


def create_bot(b: Dict[str, Any]) -> int:
    with _lock:
        cur = _conn.execute(
            "INSERT INTO bots(name, market, symbol, timeframe, mode, params, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (b["name"], b["market"], b["symbol"], b["timeframe"], b["mode"],
             json.dumps(b["params"]), int(time.time() * 1000)))
        _conn.commit()
        return cur.lastrowid


def update_bot(bot_id: int, b: Dict[str, Any]) -> None:
    with _lock:
        _conn.execute(
            "UPDATE bots SET name=?, market=?, symbol=?, timeframe=?, mode=?, params=? WHERE id=?",
            (b["name"], b["market"], b["symbol"], b["timeframe"], b["mode"],
             json.dumps(b["params"]), bot_id))
        _conn.commit()


def delete_bot(bot_id: int) -> None:
    with _lock:
        _conn.execute("DELETE FROM bots WHERE id=?", (bot_id,))
        _conn.execute("DELETE FROM trades WHERE bot_id=?", (bot_id,))
        _conn.commit()


def set_enabled(bot_id: int, enabled: bool) -> None:
    with _lock:
        _conn.execute("UPDATE bots SET enabled=? WHERE id=?", (int(enabled), bot_id))
        _conn.commit()


def save_state(bot_id: int, state: Dict[str, Any]) -> None:
    with _lock:
        _conn.execute("UPDATE bots SET state=? WHERE id=?", (json.dumps(state), bot_id))
        _conn.commit()


def add_trade(bot_id: int, t: Dict[str, Any]) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO trades(bot_id, ts, action, side, price, amount, fee, pnl, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (bot_id, t["ts"], t["action"], t["side"], t["price"], t["amount"],
             t["fee"], t.get("pnl"), t.get("reason")))
        _conn.commit()


def list_trades(bot_id: Optional[int] = None, limit: int = 200) -> List[Dict[str, Any]]:
    q = "SELECT * FROM trades"
    args: list = []
    if bot_id:
        q += " WHERE bot_id=?"
        args.append(bot_id)
    q += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(limit)
    with _lock:
        rows = _conn.execute(q, args).fetchall()
    return [dict(r) for r in rows]
