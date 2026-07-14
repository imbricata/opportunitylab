from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    ts REAL NOT NULL,
    coin TEXT NOT NULL,
    spot_coin TEXT NOT NULL,
    funding_rate REAL NOT NULL,
    perp_mark REAL NOT NULL,
    spot_mark REAL NOT NULL,
    perp_day_volume REAL NOT NULL,
    spot_day_volume REAL NOT NULL,
    basis_pct REAL NOT NULL,
    PRIMARY KEY (ts, coin)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_coin_ts
ON market_snapshots(coin, ts DESC);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    coin TEXT NOT NULL,
    spot_coin TEXT NOT NULL,
    score REAL NOT NULL,
    funding_rate REAL NOT NULL,
    median_funding_rate REAL NOT NULL,
    basis_pct REAL NOT NULL,
    projected_funding_usd REAL NOT NULL,
    roundtrip_friction_usd REAL NOT NULL,
    projected_net_usd REAL NOT NULL,
    executable_notional_usd REAL NOT NULL,
    positive_samples INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT,
    ai_decision TEXT,
    ai_confidence INTEGER,
    ai_json TEXT,
    position_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_candidates_coin_ts
ON candidates(coin, ts DESC);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    spot_coin TEXT NOT NULL,
    status TEXT NOT NULL,
    open_ts REAL NOT NULL,
    close_ts REAL,
    qty REAL NOT NULL,
    spot_entry REAL NOT NULL,
    perp_entry REAL NOT NULL,
    spot_exit REAL,
    perp_exit REAL,
    spot_cost REAL NOT NULL,
    perp_margin REAL NOT NULL,
    open_fees REAL NOT NULL,
    close_fees REAL NOT NULL DEFAULT 0,
    funding_pnl REAL NOT NULL DEFAULT 0,
    realized_pnl REAL,
    last_update_ts REAL NOT NULL,
    exit_reason TEXT,
    candidate_id INTEGER,
    FOREIGN KEY(candidate_id) REFERENCES candidates(id)
);

CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    balance REAL NOT NULL,
    position_id INTEGER,
    note TEXT
);

CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    candidate_id INTEGER,
    status TEXT NOT NULL,
    error TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def ensure_initial_balance(db: sqlite3.Connection, starting_cash: float) -> None:
    if db.execute("SELECT 1 FROM ledger LIMIT 1").fetchone() is None:
        db.execute(
            "INSERT INTO ledger(ts, kind, amount, balance, note) VALUES(?,?,?,?,?)",
            (time.time(), "initial", starting_cash, starting_cash, "virtual bankroll"),
        )
        db.commit()


def balance(db: sqlite3.Connection) -> float:
    row = db.execute("SELECT balance FROM ledger ORDER BY id DESC LIMIT 1").fetchone()
    return float(row[0]) if row else 0.0


def ledger_entry(
    db: sqlite3.Connection,
    kind: str,
    amount: float,
    position_id: int | None = None,
    note: str | None = None,
) -> float:
    new_balance = balance(db) + amount
    db.execute(
        "INSERT INTO ledger(ts, kind, amount, balance, position_id, note) VALUES(?,?,?,?,?,?)",
        (time.time(), kind, amount, new_balance, position_id, note),
    )
    return new_balance


def get_setting(db: sqlite3.Connection, key: str, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return default if row is None else json.loads(row[0])


def set_setting(db: sqlite3.Connection, key: str, value) -> None:
    db.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, separators=(",", ":"))),
    )

