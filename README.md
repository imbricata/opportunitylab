# Opportunity Lab

An autonomous, **paper-money-only** research service for short-lived market
inefficiencies. It collects public market/news data, applies deterministic
fee-aware screens, uses OpenAI only for semantic review, and records every
simulated decision in SQLite.

It has no private-key, order-signing, deposit, withdrawal, or authenticated
trading code. A wallet is not required.

## Research lanes

- Prediction-market structure: binary-complement mispricing, mutually
  exclusive NO baskets, threshold implications, and AI-audited cross-venue
  equivalents across Kalshi and Polymarket US public APIs.
- News reaction: RSS/Atom collection, local keyword filtering, then a capped AI
  review against the exact market rules. AI never receives authority to trade.
- BTC five-minute fast lane: observes Polymarket's public Chainlink RTDS stream
  and CLOB books near expiry. The opening Chainlink boundary is mandatory; a
  substitute spot price is never used. This international-market lane is
  simulation and measurement only.
- Hyperliquid funding: legacy benchmark code remains available but is disabled
  by default so this deployment focuses on prediction-market research.

All prices include modeled taker fees. Paper sizing is constrained by displayed
depth, available virtual cash, per-trade limits, and concurrent-position limits.
Missing data causes a skipped trade, not an invented value.

## Cost design

Market collection and screening use no AI tokens. SQLite holds long-term factual
memory. AI calls use structured outputs, `store: false`, low reasoning effort,
stable prompts for prompt-cache reuse, semantic result caching, and daily call
and dollar caps. The default model is `gpt-5.6-luna`.

## Commands

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m opportunity_lab.main scan
.venv/bin/python -m opportunity_lab.main status
.venv/bin/python -m opportunity_lab.main cost
.venv/bin/python -m opportunity_lab.main research
.venv/bin/python -m opportunity_lab.main run
.venv/bin/python -m unittest discover -s tests -v
```

Telegram commands: `/status`, `/cost`, `/candidates`, `/research`, `/help`.

Configuration is documented in `opportunitylab.env.example`. The service adds
tables to the existing database in place. Copy the database before deployment
if preserving a long-running experiment matters.

## Interpreting results

Paper PnL is evidence about a model, not proof of executable profit. The fast
lane is especially sensitive to network latency, queue position, partial fills,
oracle timing, and geographic/platform eligibility. A future real-money phase
would require a separate authorization and a forward paper sample large enough
to report results by strategy, entry timing, and market regime.
