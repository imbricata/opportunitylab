from __future__ import annotations

import os
import math
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

    # Strategy switches. Live execution is separately gated below and defaults off.
    enable_broad_scan: bool = _bool("LAB_ENABLE_BROAD_SCAN", False)
    enable_funding: bool = _bool("LAB_ENABLE_FUNDING", False)
    enable_kalshi: bool = _bool("LAB_ENABLE_KALSHI", True)
    enable_polymarket: bool = _bool("LAB_ENABLE_POLYMARKET", True)
    enable_polymarket_us: bool = _bool("LAB_ENABLE_POLYMARKET_US", True)
    enable_news: bool = _bool("LAB_ENABLE_NEWS", True)
    enable_fast_btc: bool = _bool("LAB_ENABLE_FAST_BTC", True)
    auto_paper_trade: bool = _bool("LAB_AUTO_PAPER_TRADE", True)

    # Strategy factory. Research workers are separate one-shot processes and
    # never receive the live wallet environment.
    factory_enabled: bool = _bool("LAB_FACTORY_ENABLED", True)
    factory_enable_market_scan: bool = _bool("LAB_FACTORY_ENABLE_MARKET_SCAN", True)
    factory_source_interval_seconds: int = _int(
        "LAB_FACTORY_SOURCE_INTERVAL_SECONDS", 21600
    )
    factory_research_interval_seconds: int = _int(
        "LAB_FACTORY_RESEARCH_INTERVAL_SECONDS", 86400
    )
    factory_max_sources_per_review: int = _int(
        "LAB_FACTORY_MAX_SOURCES_PER_REVIEW", 12
    )
    factory_ai_target_usd_per_day: float = _float(
        "LAB_FACTORY_AI_TARGET_USD_PER_DAY", 0.25
    )
    factory_feed_urls: tuple[str, ...] = tuple(
        value.strip()
        for value in os.environ.get(
            "LAB_FACTORY_FEEDS",
            "https://www.reddit.com/r/PredictionsMarkets/new/.rss?limit=25,"
            "https://www.reddit.com/r/PredictionMarkets/new/.rss?limit=25,"
            "https://www.reddit.com/r/PolymarketTrading/new/.rss?limit=25,"
            "https://www.reddit.com/r/algotrading/new/.rss?limit=25,"
            "https://www.reddit.com/r/wallstreetbets/new/.rss?limit=25",
        ).split(",")
        if value.strip()
    )
    factory_source_retention_days: int = _int(
        "LAB_FACTORY_SOURCE_RETENTION_DAYS", 14
    )
    factory_disk_pause_free_gb: float = _float(
        "LAB_FACTORY_DISK_PAUSE_FREE_GB", 2.0
    )
    factory_heartbeat_stale_seconds: int = _int(
        "LAB_FACTORY_HEARTBEAT_STALE_SECONDS", 90
    )

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
    polymarket_default_fee_coefficient: float = _float(
        "LAB_POLYMARKET_FEE_COEFFICIENT", 0.05
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
    # or submits an order. The CLOB market identity is verified once per window.
    fast_poll_seconds: float = _float("LAB_FAST_POLL_SECONDS", 3.0)
    fast_active_window_seconds: float = _float("LAB_FAST_ACTIVE_WINDOW_SECONDS", 65.0)
    fast_entry_window_seconds: float = _float("LAB_FAST_ENTRY_WINDOW_SECONDS", 11.0)
    fast_min_seconds_left: float = _float("LAB_FAST_MIN_SECONDS_LEFT", 5.0)
    fast_min_edge: float = _float("LAB_FAST_MIN_EDGE", 0.10)
    fast_unconfirmed_edge_penalty: float = _float("LAB_FAST_UNCONFIRMED_EDGE_PENALTY", 0.05)
    fast_min_margin_bps: float = _float("LAB_FAST_MIN_MARGIN_BPS", 2.5)
    fast_max_ask: float = _float("LAB_FAST_MAX_ASK", 0.80)
    fast_v8_min_confidence: float = _float("LAB_FAST_V8_MIN_CONFIDENCE", 0.70)
    fast_trade_budget_usd: float = _float("LAB_FAST_TRADE_BUDGET_USD", 5.0)
    fast_starting_cash_usd: float = _float("LAB_FAST_STARTING_CASH_USD", 20.0)
    fast_max_open_trades: int = _int("LAB_FAST_MAX_OPEN_TRADES", 1)
    fast_volatility_lookback_seconds: float = _float(
        "LAB_FAST_VOLATILITY_LOOKBACK_SECONDS", 120.0
    )
    fast_probability_logit_slope: float = _float("LAB_FAST_PROBABILITY_LOGIT_SLOPE", 0.20)
    fast_probability_cap: float = _float("LAB_FAST_PROBABILITY_CAP", 0.85)
    fast_oracle_lag_allowance_seconds: float = _float(
        "LAB_FAST_ORACLE_LAG_ALLOWANCE_SECONDS", 2.0
    )
    fast_taker_delay_seconds: float = _float("LAB_FAST_TAKER_DELAY_SECONDS", 0.25)
    fast_submission_latency_seconds: float = _float(
        "LAB_FAST_SUBMISSION_LATENCY_SECONDS", 0.10
    )
    fast_depth_safety_multiplier: float = _float("LAB_FAST_DEPTH_SAFETY_MULTIPLIER", 1.25)
    fast_max_slippage: float = _float("LAB_FAST_MAX_SLIPPAGE", 0.02)
    fast_max_book_age_seconds: float = _float("LAB_FAST_MAX_BOOK_AGE_SECONDS", 2.0)
    fast_max_execution_delay_seconds: float = _float(
        "LAB_FAST_MAX_EXECUTION_DELAY_SECONDS", 1.0
    )
    fast_min_post_delay_seconds: float = _float(
        "LAB_FAST_MIN_POST_DELAY_SECONDS", 0.75
    )
    fast_max_market_data_silence_seconds: float = _float(
        "LAB_FAST_MAX_MARKET_DATA_SILENCE_SECONDS", 5.0
    )
    fast_require_live_book: bool = _bool("LAB_FAST_REQUIRE_LIVE_BOOK", True)
    fast_min_confirming_sources: int = _int("LAB_FAST_MIN_CONFIRMING_SOURCES", 1)
    fast_v7_shadow_enabled: bool = _bool("LAB_FAST_V7_SHADOW_ENABLED", False)
    fast_v7_oracle_weight: float = _float("LAB_FAST_V7_ORACLE_WEIGHT", 0.50)
    polymarket_crypto_fee_coefficient: float = _float(
        "LAB_POLYMARKET_CRYPTO_FEE_COEFFICIENT", 0.07
    )

    # Polymarket live trading. The boolean and exact confirmation phrase must
    # agree. A positive expiry is optional; zero means no time limit. Preflight
    # checks fail closed on geography, credentials, allowance, stale balance,
    # open/unreconciled orders, and loss limits.
    live_trading_enabled: bool = _bool("LAB_LIVE_TRADING_ENABLED", False)
    live_confirmation: str = os.environ.get("LAB_LIVE_CONFIRMATION", "")
    live_armed_until: float = _float("LAB_LIVE_ARMED_UNTIL", 0.0)
    live_risk_fraction: float = _float("LAB_LIVE_RISK_FRACTION", 0.15)
    live_reduced_risk_fraction: float = _float("LAB_LIVE_REDUCED_RISK_FRACTION", 0.10)
    live_defensive_risk_fraction: float = _float("LAB_LIVE_DEFENSIVE_RISK_FRACTION", 0.07)
    live_reduce_at_drawdown: float = _float("LAB_LIVE_REDUCE_AT_DRAWDOWN", 0.10)
    live_defensive_at_drawdown: float = _float("LAB_LIVE_DEFENSIVE_AT_DRAWDOWN", 0.20)
    live_stop_at_drawdown: float = _float("LAB_LIVE_STOP_AT_DRAWDOWN", 0.25)
    # Optional low-balance bridge to the venue's minimum share quantity. It is
    # allowed only at the normal risk tier and when model confidence clears the
    # worst executable all-in unit cost by the configured margin.
    live_minimum_bridge_enabled: bool = _bool(
        "LAB_LIVE_MINIMUM_BRIDGE_ENABLED", False
    )
    live_minimum_bridge_max_fraction: float = _float(
        "LAB_LIVE_MINIMUM_BRIDGE_MAX_FRACTION", 0.24
    )
    live_minimum_bridge_min_edge: float = _float(
        "LAB_LIVE_MINIMUM_BRIDGE_MIN_EDGE", 0.03
    )
    live_reduced_bridge_max_fraction: float = _float(
        "LAB_LIVE_REDUCED_BRIDGE_MAX_FRACTION", 0.18
    )
    live_reduced_bridge_min_edge: float = _float(
        "LAB_LIVE_REDUCED_BRIDGE_MIN_EDGE", 0.06
    )
    live_require_source_confirmation: bool = _bool(
        "LAB_LIVE_REQUIRE_SOURCE_CONFIRMATION", True
    )
    live_delayed_affordability_retry: bool = _bool(
        "LAB_LIVE_DELAYED_AFFORDABILITY_RETRY", True
    )
    # A newly promoted immutable strategy may receive one separately measured
    # live-risk cohort. This is disabled by default and must be explicitly
    # enabled by the deployment that accepts automatic real-money restarts.
    live_auto_cohort_enabled: bool = _bool(
        "LAB_LIVE_AUTO_COHORT_ENABLED", False
    )
    live_auto_cohort_loss_fraction: float = _float(
        "LAB_LIVE_AUTO_COHORT_LOSS_FRACTION", 0.20
    )
    live_auto_cohort_max_loss_usd: float = _float(
        "LAB_LIVE_AUTO_COHORT_MAX_LOSS_USD", 0.0
    )
    # Reversible live circuit breaker: trips on drawdown from the lineage peak
    # since the last resume; paper keeps running and live resumes once paper
    # evidence since the trip is non-negative over enough trades.
    live_breaker_drawdown: float = _float("LAB_LIVE_BREAKER_DRAWDOWN", 0.25)
    live_breaker_resume_min_paper_trades: int = _int(
        "LAB_LIVE_BREAKER_RESUME_MIN_PAPER_TRADES", 8
    )
    # Terminal quarantine requires statistically meaningful evidence.
    live_quarantine_min_trades: int = _int("LAB_LIVE_QUARANTINE_MIN_TRADES", 30)
    live_quarantine_roi: float = _float("LAB_LIVE_QUARANTINE_ROI", -0.10)
    live_quarantine_drawdown: float = _float("LAB_LIVE_QUARANTINE_DRAWDOWN", 0.50)
    live_quarantine_paper_trades: int = _int("LAB_LIVE_QUARANTINE_PAPER_TRADES", 30)
    live_auto_cohort_cooldown_days: float = _float(
        "LAB_LIVE_AUTO_COHORT_COOLDOWN_DAYS", 7.0
    )
    live_auto_cohort_preflight_max_age_seconds: float = _float(
        "LAB_LIVE_AUTO_COHORT_PREFLIGHT_MAX_AGE_SECONDS", 300.0
    )
    live_cohort_minimum_floor_enabled: bool = _bool(
        "LAB_LIVE_COHORT_MINIMUM_FLOOR_ENABLED", False
    )
    live_cohort_trailing_loss_cap: bool = _bool(
        "LAB_LIVE_COHORT_TRAILING_LOSS_CAP", True
    )
    live_daily_loss_fraction: float = _float("LAB_LIVE_DAILY_LOSS_FRACTION", 0.18)
    live_low_balance_threshold_usd: float = _float(
        "LAB_LIVE_LOW_BALANCE_THRESHOLD_USD", 50.0
    )
    live_low_balance_daily_loss_fraction: float = _float(
        "LAB_LIVE_LOW_BALANCE_DAILY_LOSS_FRACTION", 0.30
    )
    # Optional secondary absolute limits. Zero disables them; sizing is
    # percentage-based by default so it can compound up and scale down.
    live_max_order_usd: float = _float("LAB_LIVE_MAX_ORDER_USD", 0.0)
    live_min_cash_reserve_usd: float = _float("LAB_LIVE_MIN_CASH_RESERVE_USD", 0.0)
    live_max_session_loss_usd: float = _float("LAB_LIVE_MAX_SESSION_LOSS_USD", 0.0)
    # Optional count caps. Zero disables them; loss-based controls remain active.
    live_max_orders_total: int = _int("LAB_LIVE_MAX_ORDERS_TOTAL", 0)
    live_max_orders_per_day: int = _int("LAB_LIVE_MAX_ORDERS_PER_DAY", 0)
    live_preflight_max_age_seconds: float = _float(
        "LAB_LIVE_PREFLIGHT_MAX_AGE_SECONDS", 90.0
    )
    polymarket_clob_url: str = os.environ.get(
        "POLYMARKET_CLOB_URL", "https://clob.polymarket.com"
    )
    polymarket_chain_id: int = _int("POLYMARKET_CHAIN_ID", 137)
    polymarket_signature_type: int = _int("POLYMARKET_SIGNATURE_TYPE", 0)
    polymarket_private_key: str | None = os.environ.get("POLYMARKET_PRIVATE_KEY")
    polymarket_funder: str | None = os.environ.get("POLYMARKET_FUNDER")
    polymarket_api_key: str | None = os.environ.get("POLYMARKET_CLOB_API_KEY")
    polymarket_api_secret: str | None = os.environ.get("POLYMARKET_CLOB_SECRET")
    polymarket_api_passphrase: str | None = os.environ.get("POLYMARKET_CLOB_PASSPHRASE")

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
    max_ai_calls_per_day: int = _int("LAB_MAX_AI_CALLS_PER_DAY", 100)
    # Keep a provider/pricing/error margin under the user's $1 absolute ceiling.
    max_ai_usd_per_day: float = _float("LAB_MAX_AI_USD_PER_DAY", 0.80)
    max_ai_output_tokens: int = _int("LAB_MAX_AI_OUTPUT_TOKENS", 550)
    ai_reasoning_effort: str = os.environ.get("LAB_AI_REASONING_EFFORT", "low")
    ai_service_tier: str = os.environ.get("LAB_AI_SERVICE_TIER", "flex")
    ai_timeout_seconds: int = _int("LAB_AI_TIMEOUT_SECONDS", 300)

    openai_api_key: str | None = os.environ.get("OPENAI_API_KEY")
    telegram_bot_token: str | None = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = os.environ.get("TELEGRAM_CHAT_ID")


CFG = Config()


def validate_config(cfg: Config) -> None:
    """Reject unsafe or nonsensical configuration before any worker starts."""
    fractions = {
        "live_risk_fraction": cfg.live_risk_fraction,
        "live_reduced_risk_fraction": cfg.live_reduced_risk_fraction,
        "live_defensive_risk_fraction": cfg.live_defensive_risk_fraction,
        "live_reduce_at_drawdown": cfg.live_reduce_at_drawdown,
        "live_defensive_at_drawdown": cfg.live_defensive_at_drawdown,
        "live_stop_at_drawdown": cfg.live_stop_at_drawdown,
        "live_minimum_bridge_max_fraction": cfg.live_minimum_bridge_max_fraction,
        "live_reduced_bridge_max_fraction": cfg.live_reduced_bridge_max_fraction,
        "live_auto_cohort_loss_fraction": cfg.live_auto_cohort_loss_fraction,
        "live_daily_loss_fraction": cfg.live_daily_loss_fraction,
        "live_low_balance_daily_loss_fraction": cfg.live_low_balance_daily_loss_fraction,
    }
    for name, value in fractions.items():
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{name} must be a finite fraction from 0 to 1")
    if not (
        cfg.live_reduce_at_drawdown
        <= cfg.live_defensive_at_drawdown
        <= cfg.live_stop_at_drawdown
    ):
        raise ValueError("live drawdown thresholds must be ordered")
    if cfg.live_minimum_bridge_max_fraction > cfg.live_stop_at_drawdown:
        raise ValueError("minimum bridge fraction cannot exceed the drawdown stop")
    if cfg.live_reduced_bridge_max_fraction > cfg.live_stop_at_drawdown:
        raise ValueError("reduced-tier bridge fraction cannot exceed the drawdown stop")
    for name, value in {
        "live_minimum_bridge_min_edge": cfg.live_minimum_bridge_min_edge,
        "live_reduced_bridge_min_edge": cfg.live_reduced_bridge_min_edge,
    }.items():
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{name} must be a finite fraction from 0 to 1")
    for name, value in {
        "live_auto_cohort_max_loss_usd": cfg.live_auto_cohort_max_loss_usd,
        "live_auto_cohort_cooldown_days": cfg.live_auto_cohort_cooldown_days,
        "live_auto_cohort_preflight_max_age_seconds": (
            cfg.live_auto_cohort_preflight_max_age_seconds
        ),
    }.items():
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative")
    if not math.isfinite(cfg.max_ai_usd_per_day) or not 0 <= cfg.max_ai_usd_per_day <= 1:
        raise ValueError("max_ai_usd_per_day must be between $0 and the $1 hard ceiling")
    if not math.isfinite(cfg.factory_ai_target_usd_per_day) or not (
        0 <= cfg.factory_ai_target_usd_per_day <= cfg.max_ai_usd_per_day
    ):
        raise ValueError("factory AI target must be within the daily AI ceiling")
    positive = {
        "fast_poll_seconds": cfg.fast_poll_seconds,
        "factory_source_interval_seconds": cfg.factory_source_interval_seconds,
        "factory_research_interval_seconds": cfg.factory_research_interval_seconds,
        "factory_heartbeat_stale_seconds": cfg.factory_heartbeat_stale_seconds,
        "factory_disk_pause_free_gb": cfg.factory_disk_pause_free_gb,
        "ai_timeout_seconds": cfg.ai_timeout_seconds,
    }
    for name, value in positive.items():
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if cfg.max_ai_calls_per_day < 0 or cfg.factory_max_sources_per_review < 1:
        raise ValueError("AI call and source limits are invalid")
    if cfg.ai_service_tier not in {"auto", "flex", "default"}:
        raise ValueError("ai_service_tier must be auto, flex, or default")
