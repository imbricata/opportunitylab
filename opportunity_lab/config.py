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
    timezone: str = os.environ.get("LAB_TIMEZONE", "Europe/Oslo")
    daily_digest_hour: int = _int("LAB_DAILY_DIGEST_HOUR", 20)
    max_open_positions: int = _int("LAB_MAX_OPEN_POSITIONS", 2)
    max_hold_hours: float = _float("LAB_MAX_HOLD_HOURS", 24.0)

    # Published Hyperliquid base taker rates as of 2026-07-13.
    spot_taker_fee: float = _float("LAB_SPOT_TAKER_FEE", 0.00070)
    perp_taker_fee: float = _float("LAB_PERP_TAKER_FEE", 0.00045)
    funding_retention: float = _float("LAB_FUNDING_RETENTION", 0.35)
    min_day_volume_usd: float = _float("LAB_MIN_DAY_VOLUME_USD", 100_000.0)
    min_persistent_samples: int = _int("LAB_MIN_PERSISTENT_SAMPLES", 6)
    ai_trigger_score: float = _float("LAB_AI_TRIGGER_SCORE", 70.0)
    ai_review_cooldown_hours: float = _float("LAB_AI_REVIEW_COOLDOWN_HOURS", 1.0)

    ai_model: str = os.environ.get("LAB_AI_MODEL", "gpt-5.4-nano")
    ai_required_for_entry: bool = _bool("AI_REQUIRED_FOR_ENTRY", True)
    max_ai_calls_per_day: int = _int("LAB_MAX_AI_CALLS_PER_DAY", 10)
    max_ai_usd_per_day: float = _float("LAB_MAX_AI_USD_PER_DAY", 0.03)
    max_ai_output_tokens: int = _int("LAB_MAX_AI_OUTPUT_TOKENS", 650)

    openai_api_key: str | None = os.environ.get("OPENAI_API_KEY")
    telegram_bot_token: str | None = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = os.environ.get("TELEGRAM_CHAT_ID")


CFG = Config()
