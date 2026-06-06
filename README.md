# hl-trade

A command-line interface for trading perpetuals on [Hyperliquid](https://hyperliquid.xyz).

Open and close long/short positions, set take-profit and stop-loss orders, and inspect your account — all from the terminal.

---

## Docs

- [**SETUP.md**](SETUP.md) — installation, wallet configuration, agent key setup, testnet
- [**USAGE.md**](USAGE.md) — all commands, options, and examples

## Quick start

```bash
git clone https://github.com/whynotvlad/hl-trade.git
cd hl-trade
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your keys — see SETUP.md
python main.py positions
```

## Commands

```
open        Open a long or short position
close       Close a position fully or partially
tp          Set a take-profit trigger order
sl          Set a stop-loss trigger order
cancel      Cancel a TP, SL, or any order by ID
positions   Show open positions and account balance
orders      Show all open orders
price       Show current mid price for an asset
assets      List all tradeable perpetuals
```
