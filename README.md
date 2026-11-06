# Opportunity Lab

Paper-first research and execution engine for short-lived prediction-market inefficiencies. Collects real-time order-book, oracle, and news data, runs deterministic fee- and depth-aware screens, uses bounded OpenAI calls strictly for contract review, and tracks strategy lifecycles in SQLite.

## What It Does

- **Short-horizon terminal lane:** Streams Chainlink settlement feeds, Polymarket L2 CLOB WebSockets, and Coinbase/Kraken spot prices. Models rolling TWAP settlement and simulates delayed-FOK fills across ask depth with taker fees.
- **Cross-venue screening:** Scans Polymarket, Kalshi, and Polymarket US for binary-complement mispricing, basket arbitrage, and cross-venue equivalents.
- **News review:** Filters RSS feeds locally by keyword/liquidity before structured-output LLM review against market rules (hard-capped under a daily USD budget; AI never trades directly).
- **Strategy factory & risk controls:** Evaluates immutable strategy challengers on out-of-sample forward paper cohorts before promotion. Live execution defaults off and uses drawdown-tapered sizing, circuit breakers, and least-privilege `systemd` isolation.

## Layout

```text
opportunity_lab/   # Core engine: fastlane, live execution, factory, venues, AI, SQLite
scripts/           # Offline TWAP/L2 backtests and order-book diagnostics
systemd/           # Least-privilege service and timer units
tests/             # Pytest suite (core, factory lifecycle, research models)
```

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
PYTHONPATH=. .venv/bin/python -m pytest -q
.venv/bin/python -m opportunity_lab.main status
```
