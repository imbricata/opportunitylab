# Opportunity Lab

Paper-first research and execution engine for short-lived prediction-market inefficiencies. Collects real-time order-book, oracle, and news feeds, applies deterministic fee- and depth-aware screens, uses bounded LLM calls strictly for semantic contract review, and manages an immutable strategy lifecycle in SQLite.

## Architecture & Lanes

- **Short-Horizon Terminal Lane (Polymarket CLOB + Chainlink + Spot):**
  - Observes rolling Chainlink settlement streams alongside a live L2 CLOB WebSocket book and independent spot feeds (Coinbase, Kraken).
  - Models rolling-window settlement mechanics (TWAP projection / oracle-conditioned fair value) and simulates delayed-FOK execution walking full ask depth with taker fees and latency penalties.
  - Fails closed when book age, cross-feed agreement, token identity verification, or depth cushions are violated.
- **Cross-Venue & Structural Arbitrage Screening:**
  - Scans Polymarket, Kalshi, and Polymarket US public APIs for binary-complement mispricing, mutually exclusive baskets, threshold violations, and cross-venue contract equivalents.
- **News & Semantic Review:**
  - Deterministic local keyword/liquidity filter over RSS/Atom feeds before invoking structured-output LLM review against exact market resolution rules. AI never receives direct execution authority and is hard-capped by a daily token/USD reservation budget.
- **Autonomous Strategy Factory:**
  - Registers immutable strategy specifications, replays challengers on historical delayed-FOK windows, tracks out-of-sample forward paper cohorts, and enforces sample-size, ROI, profit-factor, and drawdown gates before promotion.

## Risk & Operational Isolation

- **Paper-First Default:** Live execution is disabled unless explicit confirmation phrases, preflight checks (balance, allowance, open-order reconciliation, geoblock), and cohort risk gates pass.
- **Drawdown-Tapered Sizing & Circuit Breakers:** Live cohorts use reconstructed strategy-equity curves (immune to deposit/withdrawal distortion), tapering risk tiers across drawdown thresholds with automatic circuit breakers and strategy quarantine.
- **Least-Privilege Process Split (`systemd/`):**
  - Execution worker receives wallet credentials (`0600`) with `InaccessiblePaths` blocking AI/Telegram secrets.
  - Factory worker receives the OpenAI key with no wallet access.
  - Telegram status worker and health/maintenance timers run isolated from the decision path.

## Repository Layout

```text
opportunity_lab/
  fastlane.py           # Real-time CLOB WebSocket, Chainlink TWAP/spot streams, FOK fill model
  live.py               # Preflight checks, CLOB order signing, cohort risk & circuit breakers
  factory.py            # Immutable strategy specs, forward-cohort evaluation, promotion gates
  automation.py         # Bounded parameter challenger generation & lifecycle automation
  prediction_venues.py  # Polymarket, Kalshi, and cross-venue structural scanners
  ai.py                 # Structured-output semantic contract review & daily cost reservation
  db.py                 # SQLite schema, ledgers, cohorts, and atomic state transitions
  maintenance.py        # Integrity backups, retention pruning, and health checks
scripts/                # Offline TWAP/L2 backtests, book diagnostics, and calibration audits
systemd/                # Hardened least-privilege service and timer units
tests/                  # Unit & integration test suite (core, factory lifecycle, research models)
```

## Quick Start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# Run test suite
PYTHONPATH=. .venv/bin/python -m pytest -q

# CLI entrypoints (paper mode by default; see opportunitylab.env.example)
.venv/bin/python -m opportunity_lab.main scan
.venv/bin/python -m opportunity_lab.main status
.venv/bin/python -m opportunity_lab.main research
.venv/bin/python -m opportunity_lab.main factory-cycle
.venv/bin/python -m opportunity_lab.main run
```
