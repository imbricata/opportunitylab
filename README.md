# opportunitylab

Fake money scanner for crypto funding opportunities.

It reads live Hyperliquid data, simulates trades with a $100 virtual balance,
and uses AI to review shortlisted opportunities. It cannot place real trades.

## Commands

```bash
python3 -m opportunity_lab.main scan
python3 -m opportunity_lab.main status
python3 -m opportunity_lab.main run
python3 -m unittest discover -s tests -v
```

Telegram: `/status`, `/cost`, `/candidates`.

Configuration: `opportunitylab.env.example`.
