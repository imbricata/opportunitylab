from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import CFG, ROOT
from .db import connect, ensure_initial_balance, get_setting, set_setting
from .fastlane import PriceStream, scan_fast
from .lab import scan_all
from .reporting import (
    candidates_text,
    cost_text,
    digest_text,
    help_text,
    research_text,
    status_text,
)
from .telegram import Telegram


log = logging.getLogger("opportunity_lab")


def _next_broad_scan(now: float | None = None) -> float:
    """Align slow work just after a five-minute boundary.

    This keeps its network/AI latency away from the BTC market's final window,
    while the independent RTDS thread still captures the opening oracle tick.
    """
    now = time.time() if now is None else now
    return (int(now) // 300 + 1) * 300 + 5


def _db():
    db = connect(CFG.db_path)
    ensure_initial_balance(db, CFG.starting_cash_usd)
    return db


def run_scan(db, tg: Telegram, notify: bool = True):
    result = scan_all(db, CFG)
    log.info("scan complete: %s", result)
    if notify:
        for message in result.get("notable", []):
            tg.send(message)
        if result["paper_opened"] or result["paper_closed"]:
            tg.send(status_text(db))
    return result


def daemon():
    db = _db()
    tg = Telegram(CFG.telegram_bot_token, CFG.telegram_chat_id)
    offset = int(get_setting(db, "telegram_offset", 0))
    next_scan = 0.0
    next_fast = 0.0
    price_stream = PriceStream()
    if CFG.enable_fast_btc:
        price_stream.start()
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("daemon started; fake money only; AI configured=%s", bool(CFG.openai_api_key))
    while running:
        now = time.time()
        if now >= next_scan:
            try:
                run_scan(db, tg)
                next_scan = _next_broad_scan()
            except Exception:
                log.exception("scan failed")
                next_scan = min(time.time() + 60, _next_broad_scan())

        if now >= next_fast:
            try:
                fast_result = scan_fast(db, CFG, price_stream)
                if fast_result.get("state") not in {"observing", "sampled"}:
                    log.info("fast lane: %s", fast_result)
                for message in fast_result.get("notable", []):
                    tg.send(message)
                next_fast = now + max(1.0, CFG.fast_poll_seconds)
            except Exception:
                log.exception("fast lane failed")
                next_fast = now + 10

        local = datetime.now(ZoneInfo(CFG.timezone))
        digest_day = get_setting(db, "last_digest_day")
        if local.hour >= CFG.daily_digest_hour and digest_day != local.date().isoformat():
            try:
                tg.send(digest_text(db))
                set_setting(db, "last_digest_day", local.date().isoformat())
                db.commit()
            except Exception:
                log.exception("daily digest failed")

        try:
            if not tg.enabled:
                next_due = min(next_scan, next_fast)
                time.sleep(min(3, max(0.25, next_due - time.time())))
                continue
            for update in tg.updates(offset, timeout=1):
                offset = update["update_id"] + 1
                set_setting(db, "telegram_offset", offset)
                db.commit()
                msg = update.get("message") or {}
                if str(msg.get("chat", {}).get("id", "")) != str(CFG.telegram_chat_id):
                    continue
                text = (msg.get("text") or "").strip().lower()
                command = text.split(maxsplit=1)[0] if text else "status"
                log.info("telegram request: %s", command)
                if text.startswith("/cost"):
                    tg.send(cost_text(db))
                elif text.startswith("/help") or text.startswith("/start"):
                    tg.send(help_text())
                elif text.startswith("/research"):
                    tg.send(research_text(db))
                elif text.startswith("/candidates"):
                    tg.send(candidates_text(db))
                else:
                    tg.send(status_text(db))
                log.info("telegram response sent")
        except Exception:
            log.exception("telegram poll failed")
            time.sleep(5)
    price_stream.stop()
    db.close()


def configure_key():
    path = ROOT / ".env"
    key = getpass.getpass("OpenAI API key (hidden): ").strip()
    if not key.startswith("sk-") or len(key) < 20:
        raise SystemExit("Key does not look valid; nothing changed.")
    existing = []
    if path.exists():
        existing = [line for line in path.read_text().splitlines() if not line.startswith("OPENAI_API_KEY=")]
    existing.append(f"OPENAI_API_KEY={key}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(existing) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    print(f"Stored in {path} with mode 0600. Restart the service to load it.")


def main():
    parser = argparse.ArgumentParser(description="Fake-money opportunity research")
    parser.add_argument(
        "command",
        choices=["scan", "digest", "status", "cost", "candidates", "research", "run", "configure-key"],
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.command == "configure-key":
        configure_key()
        return
    if args.command == "run":
        daemon()
        return
    db = _db()
    tg = Telegram(CFG.telegram_bot_token, CFG.telegram_chat_id)
    if args.command == "scan":
        print(run_scan(db, tg, notify=False))
    elif args.command == "digest":
        print(digest_text(db))
    elif args.command == "status":
        print(status_text(db))
    elif args.command == "cost":
        print(cost_text(db))
    elif args.command == "candidates":
        print(candidates_text(db))
    elif args.command == "research":
        print(research_text(db))
    db.close()


if __name__ == "__main__":
    main()
