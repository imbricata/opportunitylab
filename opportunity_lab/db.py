from __future__ import annotations

import json
import hashlib
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

CREATE TABLE IF NOT EXISTS prediction_markets (
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    close_ts REAL,
    updated_ts REAL NOT NULL,
    quote_hash TEXT NOT NULL,
    market_json TEXT NOT NULL,
    PRIMARY KEY (venue, market_id)
);
CREATE INDEX IF NOT EXISTS idx_prediction_event
ON prediction_markets(venue, event_id);

CREATE TABLE IF NOT EXISTS prediction_quotes (
    ts REAL NOT NULL,
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    volume REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, venue, market_id)
);
CREATE INDEX IF NOT EXISTS idx_prediction_quotes_market
ON prediction_quotes(venue, market_id, ts DESC);

CREATE TABLE IF NOT EXISTS news_items (
    item_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    published_ts REAL NOT NULL,
    first_seen_ts REAL NOT NULL,
    summary TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_items(published_ts DESC);

CREATE TABLE IF NOT EXISTS opportunities_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    fingerprint TEXT NOT NULL,
    strategy TEXT NOT NULL,
    title TEXT NOT NULL,
    deterministic INTEGER NOT NULL,
    confidence INTEGER NOT NULL,
    unit_cost REAL NOT NULL,
    net_edge_per_unit REAL NOT NULL,
    edge_fraction REAL NOT NULL,
    max_qty REAL NOT NULL,
    opportunity_json TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT,
    ai_json TEXT,
    trade_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_opportunities_v2_fp_ts
ON opportunities_v2(fingerprint, ts DESC);
CREATE INDEX IF NOT EXISTS idx_opportunities_v2_strategy_ts
ON opportunities_v2(strategy, ts DESC);

CREATE TABLE IF NOT EXISTS paper_trades_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER,
    strategy TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    open_ts REAL NOT NULL,
    close_ts REAL,
    qty REAL NOT NULL,
    entry_cost REAL NOT NULL,
    open_fees REAL NOT NULL,
    close_fees REAL NOT NULL DEFAULT 0,
    marked_value REAL NOT NULL DEFAULT 0,
    realized_pnl REAL,
    guaranteed_payout REAL NOT NULL DEFAULT 0,
    legs_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    exit_reason TEXT,
    FOREIGN KEY(opportunity_id) REFERENCES opportunities_v2(id)
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_v2_status
ON paper_trades_v2(status, open_ts);

CREATE TABLE IF NOT EXISTS ai_cache (
    cache_key TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    created_ts REAL NOT NULL,
    expires_ts REAL NOT NULL,
    result_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_calls_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    reference_id TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS fast_price_ticks (
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    source_ts REAL NOT NULL,
    received_ts REAL NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (source, symbol, source_ts)
);
CREATE INDEX IF NOT EXISTS idx_fast_ticks_symbol_ts
ON fast_price_ticks(symbol, source_ts DESC);

CREATE TABLE IF NOT EXISTS fast_market_samples (
    ts REAL NOT NULL,
    slug TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    seconds_left REAL NOT NULL,
    price_to_beat REAL,
    reference_price REAL,
    margin_bps REAL,
    sigma_per_s REAL,
    up_probability REAL,
    up_ask REAL,
    down_ask REAL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (ts, slug)
);
CREATE INDEX IF NOT EXISTS idx_fast_samples_slug_ts
ON fast_market_samples(slug, ts DESC);

CREATE TABLE IF NOT EXISTS fast_paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    side TEXT NOT NULL,
    open_ts REAL NOT NULL,
    close_ts REAL,
    seconds_left REAL NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_fee REAL NOT NULL,
    probability REAL NOT NULL,
    model_edge REAL NOT NULL,
    price_to_beat REAL NOT NULL,
    reference_price REAL NOT NULL,
    result TEXT,
    payout REAL,
    realized_pnl REAL,
    evidence_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fast_trades_status
ON fast_paper_trades(status, open_ts);
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


def upsert_prediction_markets(db: sqlite3.Connection, markets, ts: float | None = None) -> int:
    """Store current state and sparse quote history; unchanged quotes get hourly heartbeats."""
    now = time.time() if ts is None else ts
    bucket = now - (now % 60)
    inserted_quotes = 0
    for market in markets:
        compact = market.compact()
        market_json = json.dumps(compact, sort_keys=True, separators=(",", ":"))
        quote_data = [
            market.yes_bid, market.yes_ask, market.no_bid, market.no_ask,
            round(float(market.volume), 6), market.status, market.result,
        ]
        quote_hash = hashlib.sha256(
            json.dumps(quote_data, separators=(",", ":")).encode()
        ).hexdigest()
        previous = db.execute(
            "SELECT quote_hash,updated_ts FROM prediction_markets WHERE venue=? AND market_id=?",
            (market.venue, market.market_id),
        ).fetchone()
        db.execute(
            """INSERT INTO prediction_markets
            (venue,market_id,event_id,title,category,status,close_ts,updated_ts,quote_hash,market_json)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(venue,market_id) DO UPDATE SET
              event_id=excluded.event_id,title=excluded.title,category=excluded.category,
              status=excluded.status,close_ts=excluded.close_ts,updated_ts=excluded.updated_ts,
              quote_hash=excluded.quote_hash,market_json=excluded.market_json""",
            (
                market.venue, market.market_id, market.event_id, market.title,
                market.category, market.status, market.close_ts, now, quote_hash, market_json,
            ),
        )
        changed = previous is None or previous["quote_hash"] != quote_hash
        heartbeat = previous is None or now - float(previous["updated_ts"]) >= 3600
        if changed or heartbeat:
            db.execute(
                """INSERT OR IGNORE INTO prediction_quotes
                (ts,venue,market_id,yes_bid,yes_ask,no_bid,no_ask,volume)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    bucket, market.venue, market.market_id, market.yes_bid,
                    market.yes_ask, market.no_bid, market.no_ask, market.volume,
                ),
            )
            inserted_quotes += 1
    return inserted_quotes


def load_prediction_market(db: sqlite3.Connection, venue: str, market_id: str):
    from .markets import Market

    row = db.execute(
        "SELECT market_json FROM prediction_markets WHERE venue=? AND market_id=?",
        (venue, market_id),
    ).fetchone()
    if row is None:
        return None
    data = json.loads(row[0])
    data["source_urls"] = tuple(data.get("source_urls") or ())
    return Market(**data)


def store_news_items(db: sqlite3.Connection, items) -> int:
    inserted = 0
    now = time.time()
    for item in items:
        cur = db.execute(
            """INSERT OR IGNORE INTO news_items
            (item_id,source,title,url,published_ts,first_seen_ts,summary)
            VALUES(?,?,?,?,?,?,?)""",
            (
                item.item_id, item.source, item.title, item.url,
                item.published_ts, now, item.summary,
            ),
        )
        inserted += cur.rowcount
    return inserted


def ai_cache_get(db: sqlite3.Connection, key: str):
    row = db.execute(
        "SELECT result_json FROM ai_cache WHERE cache_key=? AND expires_ts>?",
        (key, time.time()),
    ).fetchone()
    return None if row is None else json.loads(row[0])


def ai_cache_put(
    db: sqlite3.Connection, key: str, purpose: str, result: dict, ttl_seconds: float
) -> None:
    now = time.time()
    db.execute(
        """INSERT INTO ai_cache(cache_key,purpose,created_ts,expires_ts,result_json)
        VALUES(?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET
        purpose=excluded.purpose,created_ts=excluded.created_ts,
        expires_ts=excluded.expires_ts,result_json=excluded.result_json""",
        (
            key, purpose, now, now + ttl_seconds,
            json.dumps(result, sort_keys=True, separators=(",", ":")),
        ),
    )


def prune_research_data(db: sqlite3.Connection, quote_days: int = 30, news_days: int = 14) -> None:
    now = time.time()
    db.execute("DELETE FROM prediction_quotes WHERE ts<?", (now - quote_days * 86400,))
    db.execute("DELETE FROM news_items WHERE published_ts<?", (now - news_days * 86400,))
    db.execute("DELETE FROM ai_cache WHERE expires_ts<?", (now,))
    db.execute("DELETE FROM fast_price_ticks WHERE source_ts<?", (now - 2 * 86400,))
    db.execute("DELETE FROM fast_market_samples WHERE ts<?", (now - quote_days * 86400,))
