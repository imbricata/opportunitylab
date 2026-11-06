from __future__ import annotations

import argparse
import getpass
import hashlib
import logging
import os
import signal
import socket
import time

from .config import CFG, ROOT, validate_config
from .db import (
    connect,
    ensure_initial_balance,
    fast_experiment_start,
    fast_shadow_start,
    get_setting,
    set_setting,
)
from .fastlane import MarketBookStream, PriceStream, scan_fast
from .factory import (
    bootstrap_factory,
    due_notifications,
    enqueue_notification,
    factory_cycle,
    healthcheck,
    mark_notification,
    record_runtime_status,
)
from .lab import scan_all
from .live import LiveBroker
from .maintenance import run_maintenance
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
    fast_experiment_start(db)
    if CFG.fast_v7_shadow_enabled:
        fast_shadow_start(db)
    bootstrap_factory(db, CFG)
    db.commit()
    return db


def run_scan(db):
    result = scan_all(db, CFG)
    log.info("scan complete: %s", result)
    return result


def daemon():
    db = _db()
    next_fast = 0.0
    next_heartbeat = 0.0
    price_stream = PriceStream()
    book_stream = MarketBookStream()
    live_broker = LiveBroker(CFG)
    if CFG.enable_fast_btc:
        price_stream.start()
        book_stream.start()
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info(
        "daemon started; paper=%s live_gate=%s AI configured=%s",
        CFG.auto_paper_trade,
        live_broker.gate_reason() or "armed",
        bool(CFG.openai_api_key),
    )
    # Establish and verify the fail-closed health gate before the first market
    # scan can possibly submit a live order.
    set_setting(db, "btc_runtime_heartbeat", time.time())
    db.commit()
    # Never run a full-database check in the systemd Type=notify startup path.
    # Once the database became large this exceeded TimeoutStartSec and created
    # a permanent restart loop.  Verified maintenance backups own integrity
    # checks; the live worker performs only the inexpensive fail-closed checks.
    health = healthcheck(db, CFG, include_integrity=False)
    state = record_runtime_status(db, live_broker)
    _systemd_notify(
        f"READY=1\nWATCHDOG=1\nSTATUS=BTC worker {health['status']} "
        f"{state['budget_reason']}"
    )
    while running:
        now = time.time()
        if now >= next_fast:
            try:
                fast_result = scan_fast(db, CFG, price_stream, book_stream, live_broker)
                for message in fast_result.get("notable", []):
                    enqueue_notification(
                        db, kind="btc_live", message=message,
                        dedupe_key=(
                            "btc-" + hashlib.sha256(message.encode()).hexdigest()[:16]
                            + f"-{int(now // 60)}"
                        ),
                    )
                if fast_result.get("state") not in {"observing", "sampled"}:
                    log.info("fast lane: %s", fast_result)
                next_fast = now + max(1.0, CFG.fast_poll_seconds)
            except Exception:
                log.exception("fast lane failed")
                next_fast = now + 10
        if now >= next_heartbeat:
            try:
                state = record_runtime_status(db, live_broker)
                _systemd_notify(
                    f"WATCHDOG=1\nSTATUS=BTC worker {state['budget_reason']}"
                )
            except Exception:
                log.exception("runtime heartbeat failed")
            next_heartbeat = now + 15
        time.sleep(min(1.0, max(0.1, next_fast - time.time())))
    _systemd_notify("STOPPING=1\nSTATUS=BTC worker stopping")
    price_stream.stop()
    book_stream.stop()
    live_broker.shutdown(db)
    db.close()


def _systemd_notify(message: str) -> bool:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.connect(address)
        sock.sendall(message.encode())
        return True
    except OSError:
        log.exception("systemd notify failed")
        return False
    finally:
        sock.close()


def telegram_daemon():
    db = _db()
    tg = Telegram(CFG.telegram_bot_token, CFG.telegram_chat_id)
    if not tg.enabled:
        raise SystemExit("Telegram token/chat ID are not configured")
    offset = int(get_setting(db, "telegram_offset", 0))
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    tg.keep_status_command_only()
    log.info("Telegram status worker started")
    while running:
        try:
            for row in due_notifications(db):
                try:
                    tg.send(row["message"])
                    mark_notification(db, row["id"])
                except Exception as exc:
                    mark_notification(db, row["id"], exc)
                    raise
            for update in tg.updates(offset, timeout=20):
                offset = update["update_id"] + 1
                set_setting(db, "telegram_offset", offset)
                db.commit()
                msg = update.get("message") or {}
                if str(msg.get("chat", {}).get("id", "")) != str(CFG.telegram_chat_id):
                    continue
                text = (msg.get("text") or "").strip().lower()
                command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
                if command == "/status":
                    tg.send(status_text(db))
        except Exception:
            log.exception("telegram worker cycle failed")
            time.sleep(5)
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


def configure_live_wallet():
    """Securely configure a funded Polymarket smart wallet.

    This command is intentionally interactive and remains disarmed. The secret
    is read with getpass, the wallet type is checked through Polymarket's public
    relayer, authenticated balance endpoints validate signer/funder ownership,
    and configuration is written atomically to a mode-0600 .live.env.
    """
    import json
    import urllib.parse
    import urllib.request

    from eth_account import Account
    from py_clob_client_v2 import (
        AssetType,
        BalanceAllowanceParams,
        ClobClient,
    )

    from .live import _minimum_allowance, _usd_units

    funder = input("Polymarket wallet/funder address: ").strip().lower()
    if not funder.startswith("0x") or len(funder) != 42:
        raise SystemExit("Funder address is invalid; nothing changed.")
    private_key = getpass.getpass("Exported Magic wallet private key (hidden): ").strip()
    try:
        signer = Account.from_key(private_key)
    except Exception:
        raise SystemExit("Private key is invalid; nothing changed.") from None

    def deployed(wallet_type: str) -> bool:
        url = "https://relayer-v2.polymarket.com/deployed?" + urllib.parse.urlencode(
            {"address": funder, "type": wallet_type}
        )
        request = urllib.request.Request(
            url, headers={"User-Agent": "opportunitylab-live-setup/1.0"}
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return bool(json.load(response).get("deployed"))

    if deployed("WALLET"):
        signature_type = 3  # New deposit wallet / POLY_1271.
        wallet_label = "POLY_1271 deposit wallet"
    elif deployed("SAFE"):
        signature_type = 2
        wallet_label = "Gnosis Safe"
    else:
        signature_type = 1  # Legacy POLY_PROXY.
        wallet_label = "POLY_PROXY"

    host = "https://clob.polymarket.com"
    chain_id = 137
    temp = ClobClient(host=host, chain_id=chain_id, key=private_key)
    creds = temp.create_or_derive_api_key()
    client = ClobClient(
        host=host,
        chain_id=chain_id,
        key=private_key,
        creds=creds,
        signature_type=signature_type,
        funder=funder,
        retry_on_error=False,
    )
    params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL, signature_type=signature_type
    )
    client.update_balance_allowance(params)
    funds = client.get_balance_allowance(params)
    open_orders = client.get_open_orders(only_first_page=True)
    balance = _usd_units(funds.get("balance"))
    allowance = _minimum_allowance(funds)
    if balance <= 0:
        raise SystemExit(
            "Authenticated signer/funder check returned no pUSD. Nothing was stored; "
            "verify the exported account and funder address."
        )

    path = ROOT / ".live.env"
    values = {
        "LAB_LIVE_TRADING_ENABLED": "0",
        "LAB_LIVE_CONFIRMATION": "",
        "LAB_LIVE_ARMED_UNTIL": "0",
        "POLYMARKET_SIGNATURE_TYPE": str(signature_type),
        "POLYMARKET_FUNDER": funder,
        "POLYMARKET_PRIVATE_KEY": private_key,
        "POLYMARKET_CLOB_API_KEY": creds.api_key,
        "POLYMARKET_CLOB_SECRET": creds.api_secret,
        "POLYMARKET_CLOB_PASSPHRASE": creds.api_passphrase,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    print(f"Signer: {signer.address}")
    print(f"Funder: {funder}")
    print(f"Wallet type: {signature_type} ({wallet_label})")
    print(f"Authenticated pUSD: ${balance:.2f}; allowance: ${allowance:.2f}")
    print(f"Open venue orders: {len(open_orders)}")
    print(f"Stored in {path} with mode 0600. LIVE TRADING REMAINS DISARMED.")


def main():
    parser = argparse.ArgumentParser(description="Fake-money opportunity research")
    parser.add_argument(
        "command",
        choices=[
            "scan", "digest", "status", "cost", "candidates", "research", "run",
            "configure-key", "configure-live-wallet", "configure-live-proxy",
            "telegram-run", "factory-cycle", "maintenance", "healthcheck",
            "automation-cycle",
        ],
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    validate_config(CFG)
    if args.command == "configure-key":
        configure_key()
        return
    if args.command in {"configure-live-wallet", "configure-live-proxy"}:
        configure_live_wallet()
        return
    if args.command == "run":
        daemon()
        return
    if args.command == "telegram-run":
        telegram_daemon()
        return
    db = _db()
    if args.command == "scan":
        print(run_scan(db))
    elif args.command == "factory-cycle":
        print(factory_cycle(db, CFG))
    elif args.command == "automation-cycle":
        from .automation import automation_cycle

        print(automation_cycle(db, CFG))
    elif args.command == "maintenance":
        print(run_maintenance(db, CFG))
    elif args.command == "healthcheck":
        print(healthcheck(db, CFG, include_integrity=False))
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
