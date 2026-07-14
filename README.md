# Opportunity Lab

Fake-money scanner for spot/perpetual funding opportunities.

- Reads public Hyperliquid production data.
- Simulates executable depth, spread, fees, funding, and exits.
- Stores observations and virtual trades in SQLite.
- Uses no wallet, signer, trading endpoint, or exchange credential.
- Calls `gpt-5.4-nano` only after deterministic screening.
- Retains no model conversation and uses no model web search.
- Limits AI to 10 attempts and an estimated $0.03 per Oslo day.

The scanner runs every five minutes. A same-coin AI review waits one hour
unless rank rises 10 points or projected net profit rises at least 50% and
$0.03.

```bash
python3 -m opportunity_lab.main scan
python3 -m opportunity_lab.main status
python3 -m opportunity_lab.main cost
python3 -m opportunity_lab.main run
python3 -m unittest discover -s tests -v
```

Configuration is documented in `opportunity-lab.env.example`. Telegram accepts
`/status`, `/cost`, and `/candidates` and sends a daily 20:00 Oslo digest.

The rank is a triage score, not a probability of profit.
