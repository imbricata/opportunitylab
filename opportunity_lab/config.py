from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    db_path: Path = Path(os.environ.get("LAB_DB_PATH", ROOT / "opportunity_lab.db"))
    starting_cash_usd: float = _float("LAB_STARTING_CASH_USD", 100.0)
    position_notional_usd: float = _float("LAB_POSITION_NOTIONAL_USD", 15.0)
    scan_interval_seconds: int = _int("LAB_SCAN_INTERVAL_SECONDS", 300)
    timezone: str = os.environ.get("LAB_TIMEZONE", "America/New_York")
    daily_digest_hour: int = _int("LAB_DAILY_DIGEST_HOUR", 20)
    max_open_positions: int = _int("LAB_MAX_OPEN_POSITIONS", 2)
    max_hold_hours: float = _float("LAB_MAX_HOLD_HOURS", 24.0)

    # Strategy switches. Every execution path remains simulated.
    enable_funding: bool = _bool("LAB_ENABLE_FUNDING", False)
    enable_kalshi: bool = _bool("LAB_ENABLE_KALSHI", True)
    enable_polymarket_us: bool = _bool("LAB_ENABLE_POLYMARKET_US", True)
    enable_news: bool = _bool("LAB_ENABLE_NEWS", True)
    enable_fast_btc: bool = _bool("LAB_ENABLE_FAST_BTC", True)
    auto_paper_trade: bool = _bool("LAB_AUTO_PAPER_TRADE", True)

    # Prediction-market collection and deterministic opportunity screening.
    kalshi_event_limit: int = _int("LAB_KALSHI_EVENT_LIMIT", 200)
    polymarket_market_limit: int = _int("LAB_POLYMARKET_MARKET_LIMIT", 300)
    prediction_min_volume_usd: float = _float("LAB_PREDICTION_MIN_VOLUME_USD", 50.0)
    prediction_min_net_edge: float = _float("LAB_PREDICTION_MIN_NET_EDGE", 0.015)
    cross_venue_similarity: float = _float("LAB_CROSS_VENUE_SIMILARITY", 0.67)
    cross_venue_ai_confidence: int = _int("LAB_CROSS_VENUE_AI_CONFIDENCE", 90)
    opportunity_cooldown_minutes: int = _int("LAB_OPPORTUNITY_COOLDOWN_MINUTES", 60)

    # Shared fake wallet and generic paper-execution controls.
    paper_trade_budget_usd: float = _float("LAB_PAPER_TRADE_BUDGET_USD", 5.0)
    paper_max_allocation_usd: float = _float("LAB_PAPER_MAX_ALLOCATION_USD", 40.0)
    paper_max_open_trades: int = _int("LAB_PAPER_MAX_OPEN_TRADES", 8)
    paper_max_strategy_trades: int = _int("LAB_PAPER_MAX_STRATEGY_TRADES", 3)
    paper_arb_max_hold_hours: float = _float("LAB_PAPER_ARB_MAX_HOLD_HOURS", 168.0)
    paper_directional_max_hold_hours: float = _float("LAB_PAPER_DIRECTIONAL_MAX_HOLD_HOURS", 30.0)
    paper_directional_stop_pct: float = _float("LAB_PAPER_DIRECTIONAL_STOP_PCT", 0.30)
    paper_directional_take_pct: float = _float("LAB_PAPER_DIRECTIONAL_TAKE_PCT", 0.35)
    kalshi_taker_fee_coefficient: float = _float("LAB_KALSHI_TAKER_FEE_COEFFICIENT", 0.07)
    polymarket_us_default_fee_coefficient: float = _float(
        "LAB_POLYMARKET_US_FEE_COEFFICIENT", 0.05
    )

    # News is collected locally first. AI sees only items with deterministic
    # keyword overlap to an active, liquid market.
    news_interval_seconds: int = _int("LAB_NEWS_INTERVAL_SECONDS", 1800)
    news_max_age_hours: float = _float("LAB_NEWS_MAX_AGE_HOURS", 18.0)
    news_query_market_count: int = _int("LAB_NEWS_QUERY_MARKET_COUNT", 8)
    news_ai_edge: float = _float("LAB_NEWS_AI_EDGE", 0.12)
    news_ai_confidence: int = _int("LAB_NEWS_AI_CONFIDENCE", 75)
    news_feed_urls: tuple[str, ...] = tuple(
        value.strip()
        for value in os.environ.get(
            "LAB_NEWS_FEEDS",
            "https://www.federalreserve.gov/feeds/press_all.xml,"
            "https://www.sec.gov/news/pressreleases.rss,"
            "https://feeds.bbci.co.uk/news/world/rss.xml",
        ).split(",")
        if value.strip()
    )

    # Paper-only Polymarket international research lane. It observes the
    # Chainlink reference stream that resolves the contract, but never signs
    # or submits an order. REST book polling activates only near expiry.
    fast_poll_seconds: float = _float("LAB_FAST_POLL_SECONDS", 3.0)
    fast_active_window_seconds: float = _float("LAB_FAST_ACTIVE_WINDOW_SECONDS", 45.0)
    fast_entry_window_seconds: float = _float("LAB_FAST_ENTRY_WINDOW_SECONDS", 20.0)
    fast_min_seconds_left: float = _float("LAB_FAST_MIN_SECONDS_LEFT", 2.5)
    fast_min_edge: float = _float("LAB_FAST_MIN_EDGE", 0.06)
    fast_unconfirmed_edge_penalty: float = _float("LAB_FAST_UNCONFIRMED_EDGE_PENALTY", 0.03)
    fast_min_margin_bps: float = _float("LAB_FAST_MIN_MARGIN_BPS", 1.5)
    fast_max_ask: float = _float("LAB_FAST_MAX_ASK", 0.92)
    fast_trade_budget_usd: float = _float("LAB_FAST_TRADE_BUDGET_USD", 5.0)
    fast_max_open_trades: int = _int("LAB_FAST_MAX_OPEN_TRADES", 2)
    fast_volatility_lookback_seconds: float = _float(
        "LAB_FAST_VOLATILITY_LOOKBACK_SECONDS", 120.0
    )
    fast_probability_shrink: float = _float("LAB_FAST_PROBABILITY_SHRINK", 0.70)
    polymarket_crypto_fee_coefficient: float = _float(
        "LAB_POLYMARKET_CRYPTO_FEE_COEFFICIENT", 0.07
    )

    # Published Hyperliquid base taker rates as of 2026-07-13.
    spot_taker_fee: float = _float("LAB_SPOT_TAKER_FEE", 0.00070)
    perp_taker_fee: float = _float("LAB_PERP_TAKER_FEE", 0.00045)
    funding_retention: float = _float("LAB_FUNDING_RETENTION", 0.35)
    min_day_volume_usd: float = _float("LAB_MIN_DAY_VOLUME_USD", 100_000.0)
    min_persistent_samples: int = _int("LAB_MIN_PERSISTENT_SAMPLES", 6)
    ai_trigger_score: float = _float("LAB_AI_TRIGGER_SCORE", 70.0)
    ai_review_cooldown_hours: float = _float("LAB_AI_REVIEW_COOLDOWN_HOURS", 1.0)

    ai_model: str = os.environ.get("LAB_AI_MODEL", "gpt-5.6-luna")
    ai_required_for_entry: bool = _bool("AI_REQUIRED_FOR_ENTRY", True)
    max_ai_calls_per_day: int = _int("LAB_MAX_AI_CALLS_PER_DAY", 20)
    max_ai_usd_per_day: float = _float("LAB_MAX_AI_USD_PER_DAY", 0.10)
    max_ai_output_tokens: int = _int("LAB_MAX_AI_OUTPUT_TOKENS", 550)
    ai_reasoning_effort: str = os.environ.get("LAB_AI_REASONING_EFFORT", "low")

    openai_api_key: str | None = os.environ.get("OPENAI_API_KEY")
    telegram_bot_token: str | None = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = os.environ.get("TELEGRAM_CHAT_ID")


CFG = Config()
