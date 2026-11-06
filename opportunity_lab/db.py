from __future__ import annotations

import json
import hashlib
import sqlite3
import time
from pathlib import Path


FAST_EXPERIMENT_VERSION = "market-conditioned-confidence70-cap80-final11-v8"
FAST_SHADOW_VERSION = "oracle50-market50-book50-fokdepth-v7"
BTC_V8_STRATEGY_ID = "btc_5m_v8"
BTC_V6_STRATEGY_ID = "btc_5m_v6"

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
CREATE INDEX IF NOT EXISTS idx_fast_ticks_source_ts
ON fast_price_ticks(source_ts);

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

CREATE TABLE IF NOT EXISTS fast_execution_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    slug TEXT NOT NULL,
    side TEXT NOT NULL,
    seconds_left REAL NOT NULL,
    decision_ask REAL NOT NULL,
    delayed_ask REAL,
    requested_qty REAL NOT NULL,
    available_qty REAL NOT NULL,
    delay_ms REAL NOT NULL,
    quote_age_ms REAL,
    modeled_probability REAL NOT NULL,
    pre_delay_edge REAL NOT NULL,
    post_delay_edge REAL,
    outcome TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fast_attempts_slug_ts
ON fast_execution_attempts(slug, ts DESC);

CREATE TABLE IF NOT EXISTS fast_live_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    state TEXT NOT NULL,
    requested_qty REAL NOT NULL,
    limit_price REAL NOT NULL,
    risk_budget_usd REAL NOT NULL,
    balance_before_usd REAL NOT NULL,
    actual_qty REAL,
    actual_cost_usd REAL,
    order_id TEXT,
    venue_status TEXT,
    response_json TEXT,
    error TEXT,
    result TEXT,
    realized_pnl REAL,
    evidence_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fast_live_orders_state_ts
ON fast_live_orders(state, created_ts DESC);

CREATE TABLE IF NOT EXISTS fast_screen_decisions (
    ts REAL NOT NULL,
    slug TEXT NOT NULL,
    side TEXT NOT NULL,
    seconds_left REAL NOT NULL,
    ask REAL,
    fair_value REAL NOT NULL,
    fee_per_share REAL,
    net_edge REAL,
    required_edge REAL NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (ts, slug)
);
CREATE INDEX IF NOT EXISTS idx_fast_screens_reason_ts
ON fast_screen_decisions(reason, ts DESC);

CREATE TABLE IF NOT EXISTS fast_shadow_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_version TEXT NOT NULL,
    slug TEXT NOT NULL,
    status TEXT NOT NULL,
    side TEXT NOT NULL,
    open_ts REAL NOT NULL,
    close_ts REAL,
    seconds_left REAL NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_fee REAL NOT NULL,
    oracle_probability REAL NOT NULL,
    market_probability REAL NOT NULL,
    blended_probability REAL NOT NULL,
    model_edge REAL NOT NULL,
    result TEXT,
    payout REAL,
    realized_pnl REAL,
    evidence_json TEXT NOT NULL,
    UNIQUE(strategy_version, slug)
);
CREATE INDEX IF NOT EXISTS idx_fast_shadow_trades_status
ON fast_shadow_trades(strategy_version, status, open_ts);

CREATE TABLE IF NOT EXISTS fast_shadow_decisions (
    ts REAL NOT NULL,
    strategy_version TEXT NOT NULL,
    slug TEXT NOT NULL,
    side TEXT NOT NULL,
    seconds_left REAL NOT NULL,
    ask REAL,
    oracle_probability REAL NOT NULL,
    market_probability REAL,
    blended_probability REAL,
    fee_per_share REAL,
    net_edge REAL,
    required_edge REAL NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (ts, strategy_version, slug)
);
CREATE INDEX IF NOT EXISTS idx_fast_shadow_decisions_reason
ON fast_shadow_decisions(strategy_version, reason, ts DESC);

CREATE TABLE IF NOT EXISTS fast_window_results (
    slug TEXT PRIMARY KEY,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    opening_price REAL,
    ending_price REAL,
    observed_result TEXT,
    venue_result TEXT NOT NULL,
    result_matches INTEGER,
    validated_ts REAL NOT NULL,
    evidence_json TEXT NOT NULL
);
"""


FACTORY_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        ALTER TABLE fast_paper_trades ADD COLUMN strategy_version_id TEXT NOT NULL DEFAULT 'legacy';
        ALTER TABLE fast_live_orders ADD COLUMN strategy_version_id TEXT NOT NULL DEFAULT 'legacy';

        CREATE TABLE factory_strategy_versions (
            strategy_version_id TEXT PRIMARY KEY,
            strategy_key TEXT NOT NULL,
            version TEXT NOT NULL,
            name TEXT NOT NULL,
            venue TEXT NOT NULL,
            family TEXT NOT NULL,
            lifecycle TEXT NOT NULL,
            live_allowed INTEGER NOT NULL DEFAULT 0,
            created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL,
            code_hash TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            notes TEXT,
            UNIQUE(strategy_key, version)
        );
        CREATE INDEX idx_factory_strategies_lifecycle
        ON factory_strategy_versions(lifecycle, updated_ts DESC);

        CREATE TABLE factory_allocations (
            strategy_version_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            max_risk_fraction REAL NOT NULL DEFAULT 0,
            max_order_usd REAL NOT NULL DEFAULT 0,
            weight REAL NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL,
            reason TEXT NOT NULL,
            FOREIGN KEY(strategy_version_id) REFERENCES factory_strategy_versions(strategy_version_id)
        );

        CREATE TABLE factory_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            scheduled_ts REAL NOT NULL,
            started_ts REAL NOT NULL,
            finished_ts REAL,
            state TEXT NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}',
            error TEXT
        );
        CREATE INDEX idx_factory_runs_kind_ts ON factory_runs(kind, started_ts DESC);

        CREATE TABLE factory_sources (
            source_id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            source_name TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            published_ts REAL NOT NULL,
            discovered_ts REAL NOT NULL,
            content_hash TEXT NOT NULL,
            summary TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            status TEXT NOT NULL,
            credibility REAL NOT NULL DEFAULT 0,
            expires_ts REAL NOT NULL
        );
        CREATE INDEX idx_factory_sources_status_ts
        ON factory_sources(status, published_ts DESC);
        CREATE INDEX idx_factory_sources_hash ON factory_sources(content_hash);

        CREATE TABLE factory_hypotheses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis_key TEXT NOT NULL UNIQUE,
            created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL,
            title TEXT NOT NULL,
            venue TEXT NOT NULL,
            family TEXT NOT NULL,
            thesis TEXT NOT NULL,
            required_data TEXT NOT NULL,
            deterministic_test TEXT NOT NULL,
            risks_json TEXT NOT NULL,
            source_ids_json TEXT NOT NULL,
            confidence INTEGER NOT NULL,
            lifecycle TEXT NOT NULL,
            strategy_version_id TEXT,
            review_json TEXT NOT NULL,
            FOREIGN KEY(strategy_version_id) REFERENCES factory_strategy_versions(strategy_version_id)
        );
        CREATE INDEX idx_factory_hypotheses_lifecycle
        ON factory_hypotheses(lifecycle, updated_ts DESC);

        CREATE TABLE factory_metrics (
            strategy_version_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            ts REAL NOT NULL,
            trades INTEGER NOT NULL,
            wins INTEGER NOT NULL,
            open_trades INTEGER NOT NULL,
            pnl REAL NOT NULL,
            capital_risked REAL NOT NULL,
            roi REAL NOT NULL,
            metrics_json TEXT NOT NULL,
            PRIMARY KEY(strategy_version_id, mode, ts),
            FOREIGN KEY(strategy_version_id) REFERENCES factory_strategy_versions(strategy_version_id)
        );
        CREATE INDEX idx_factory_metrics_strategy_ts
        ON factory_metrics(strategy_version_id, ts DESC);

        CREATE TABLE factory_capital_snapshots (
            ts REAL PRIMARY KEY,
            wallet_balance REAL NOT NULL,
            session_initial REAL NOT NULL,
            realized_live_pnl REAL NOT NULL,
            estimated_external_net REAL NOT NULL,
            pending_orders INTEGER NOT NULL,
            detail_json TEXT NOT NULL
        );

        CREATE TABLE factory_health (
            check_name TEXT PRIMARY KEY,
            checked_ts REAL NOT NULL,
            status TEXT NOT NULL,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL
        );

        CREATE TABLE notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_ts REAL NOT NULL,
            due_ts REAL NOT NULL,
            severity TEXT NOT NULL,
            kind TEXT NOT NULL,
            dedupe_key TEXT NOT NULL UNIQUE,
            message TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            sent_ts REAL,
            last_error TEXT
        );
        CREATE INDEX idx_notification_outbox_due
        ON notification_outbox(sent_ts, due_ts);

        CREATE INDEX idx_ai_calls_v2_ts ON ai_calls_v2(ts);
        CREATE INDEX idx_fast_ticks_source_source_ts
        ON fast_price_ticks(source, source_ts DESC);

        UPDATE fast_paper_trades
        SET strategy_version_id='btc_5m_v8'
        WHERE json_extract(evidence_json,'$.model_version')='market-conditioned-confidence70-cap80-final11-v8';
        UPDATE fast_live_orders
        SET strategy_version_id='btc_5m_v8'
        WHERE json_extract(evidence_json,'$.model_version')='market-conditioned-confidence70-cap80-final11-v8';
        """,
    ),
    (
        2,
        """
        UPDATE fast_paper_trades
        SET strategy_version_id='btc_5m_v6'
        WHERE strategy_version_id='legacy'
          AND json_extract(evidence_json,'$.model_version')='logit20-cap85-book50-fokdepth-latency1s-bankroll20-v6';
        UPDATE fast_live_orders
        SET strategy_version_id='btc_5m_v6'
        WHERE strategy_version_id='legacy'
          AND json_extract(evidence_json,'$.model_version')='logit20-cap85-book50-fokdepth-latency1s-bankroll20-v6';
        """,
    ),
    (
        3,
        """
        CREATE TABLE factory_strategy_specs (
            spec_id TEXT PRIMARY KEY,
            strategy_version_id TEXT NOT NULL UNIQUE,
            hypothesis_id INTEGER,
            adapter TEXT NOT NULL,
            family TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            parent_spec_id TEXT,
            created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL,
            state TEXT NOT NULL,
            live_capable INTEGER NOT NULL DEFAULT 0,
            parameters_json TEXT NOT NULL,
            data_requirements_json TEXT NOT NULL,
            spec_hash TEXT NOT NULL UNIQUE,
            blocked_reason TEXT,
            FOREIGN KEY(strategy_version_id) REFERENCES factory_strategy_versions(strategy_version_id),
            FOREIGN KEY(hypothesis_id) REFERENCES factory_hypotheses(id),
            FOREIGN KEY(parent_spec_id) REFERENCES factory_strategy_specs(spec_id)
        );
        CREATE INDEX idx_factory_specs_state_adapter
        ON factory_strategy_specs(state,adapter,updated_ts DESC);

        CREATE TABLE factory_variant_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            spec_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            slug TEXT NOT NULL,
            signal_ts REAL NOT NULL,
            settled_ts REAL NOT NULL,
            side TEXT NOT NULL,
            seconds_left REAL NOT NULL,
            confidence REAL NOT NULL,
            entry_price REAL NOT NULL,
            qty REAL NOT NULL,
            cost REAL NOT NULL,
            result TEXT NOT NULL,
            pnl REAL NOT NULL,
            evidence_json TEXT NOT NULL,
            UNIQUE(spec_id,mode,slug),
            FOREIGN KEY(spec_id) REFERENCES factory_strategy_specs(spec_id)
        );
        CREATE INDEX idx_factory_variant_spec_mode_ts
        ON factory_variant_trades(spec_id,mode,signal_ts DESC);

        CREATE TABLE factory_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            spec_id TEXT NOT NULL,
            ts REAL NOT NULL,
            replay_trades INTEGER NOT NULL,
            forward_trades INTEGER NOT NULL,
            forward_days REAL NOT NULL,
            wins INTEGER NOT NULL,
            pnl REAL NOT NULL,
            capital_risked REAL NOT NULL,
            roi REAL NOT NULL,
            max_drawdown REAL NOT NULL,
            profit_factor REAL NOT NULL,
            score REAL NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            FOREIGN KEY(spec_id) REFERENCES factory_strategy_specs(spec_id)
        );
        CREATE INDEX idx_factory_evaluations_spec_ts
        ON factory_evaluations(spec_id,ts DESC);

        CREATE TABLE factory_lifecycle_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            spec_id TEXT,
            from_state TEXT,
            to_state TEXT NOT NULL,
            reason TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            FOREIGN KEY(spec_id) REFERENCES factory_strategy_specs(spec_id)
        );
        CREATE INDEX idx_factory_lifecycle_ts
        ON factory_lifecycle_events(ts DESC);
        """,
    ),
    (
        4,
        """
        CREATE TABLE fast_live_shadow_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            strategy_version_id TEXT NOT NULL,
            side TEXT NOT NULL,
            gate_reason TEXT NOT NULL,
            source_confirmed INTEGER,
            base_budget_usd REAL NOT NULL,
            effective_budget_usd REAL NOT NULL,
            minimum_qty REAL NOT NULL,
            decision_limit_price REAL,
            delayed_ask REAL,
            delayed_minimum_cost_usd REAL,
            candidate_qty REAL NOT NULL,
            outcome TEXT NOT NULL,
            evidence_json TEXT NOT NULL
        );
        CREATE INDEX idx_fast_live_shadow_outcome_ts
        ON fast_live_shadow_decisions(outcome, ts DESC);
        """,
    ),
    (
        5,
        """
        ALTER TABLE fast_live_orders ADD COLUMN risk_cohort_id INTEGER;

        CREATE TABLE live_risk_cohorts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_version_id TEXT NOT NULL UNIQUE,
            created_ts REAL NOT NULL,
            state TEXT NOT NULL,
            authorization_source TEXT NOT NULL,
            wallet_balance_start_usd REAL NOT NULL,
            loss_cap_usd REAL NOT NULL,
            cooldown_until_ts REAL NOT NULL,
            ended_ts REAL,
            stop_reason TEXT,
            evidence_json TEXT NOT NULL,
            FOREIGN KEY(strategy_version_id)
              REFERENCES factory_strategy_versions(strategy_version_id)
        );
        CREATE UNIQUE INDEX idx_live_risk_cohort_one_active
        ON live_risk_cohorts(state) WHERE state='active';
        CREATE INDEX idx_live_risk_cohort_strategy_ts
        ON live_risk_cohorts(strategy_version_id,created_ts DESC);
        CREATE INDEX idx_fast_live_orders_risk_cohort
        ON fast_live_orders(risk_cohort_id,created_ts);
        """,
    ),
)


def _apply_factory_migrations(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_ts REAL NOT NULL
        )"""
    )
    applied = {
        int(row["version"]): row["checksum"]
        for row in db.execute("SELECT version,checksum FROM schema_migrations")
    }
    for version, sql in FACTORY_MIGRATIONS:
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        if version in applied:
            if applied[version] != checksum:
                raise RuntimeError(f"factory migration {version} checksum mismatch")
            continue
        # Recheck after taking the write lock so simultaneous service starts
        # cannot race. SQLite schema changes are transactional, so a crash
        # between ALTER statements rolls the whole migration back.
        db.commit()
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT checksum FROM schema_migrations WHERE version=?", (version,)
            ).fetchone()
            if existing is not None:
                if existing[0] != checksum:
                    raise RuntimeError(f"factory migration {version} checksum mismatch")
                db.commit()
                continue
            for statement in sql.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute(
                "INSERT INTO schema_migrations(version,checksum,applied_ts) VALUES(?,?,?)",
                (version, checksum, time.time()),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    _apply_factory_migrations(db)
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


def fast_experiment_start(db: sqlite3.Connection) -> float:
    """Stable cutoff separating hardened live-book evidence from legacy paper runs."""
    version = get_setting(db, "fast_experiment_version")
    value = get_setting(db, "fast_livebook_experiment_start")
    if value is None or version != FAST_EXPERIMENT_VERSION:
        value = time.time()
        set_setting(db, "fast_livebook_experiment_start", value)
        set_setting(db, "fast_experiment_version", FAST_EXPERIMENT_VERSION)
    return float(value)


def fast_experiment_balance(db: sqlite3.Connection, starting_cash: float) -> float:
    """Cohort cash from its own trades, independent of delayed legacy settlements."""
    started = fast_experiment_start(db)
    movement = db.execute(
        """SELECT COALESCE(SUM(
          -(qty*entry_price+entry_fee)
          + CASE WHEN status='settled' THEN COALESCE(payout,0) ELSE 0 END
        ),0) FROM fast_paper_trades WHERE open_ts>=?""",
        (started,),
    ).fetchone()[0]
    return float(starting_cash) + float(movement or 0)


def fast_shadow_start(db: sqlite3.Connection) -> float:
    """Stable start for the isolated v7 forward-test cohort."""
    key = "fast_shadow_v7_start"
    version_key = "fast_shadow_v7_version"
    version = get_setting(db, version_key)
    value = get_setting(db, key)
    if value is None or version != FAST_SHADOW_VERSION:
        value = time.time()
        set_setting(db, key, value)
        set_setting(db, version_key, FAST_SHADOW_VERSION)
    return float(value)


def fast_shadow_balance(db: sqlite3.Connection, starting_cash: float) -> float:
    started = fast_shadow_start(db)
    movement = db.execute(
        """SELECT COALESCE(SUM(
          -(qty*entry_price+entry_fee)
          + CASE WHEN status='settled' THEN COALESCE(payout,0) ELSE 0 END
        ),0) FROM fast_shadow_trades
        WHERE strategy_version=? AND open_ts>=?""",
        (FAST_SHADOW_VERSION, started),
    ).fetchone()[0]
    return float(starting_cash) + float(movement or 0)


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
