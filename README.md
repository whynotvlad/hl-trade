# hl-trade

A command-line interface for trading perpetuals on [Hyperliquid](https://hyperliquid.xyz) — open and close long/short positions, set take-profit and stop-loss orders, and inspect your account, all from the terminal.

---

## Requirements

- Python 3.10+
- A Hyperliquid account with USDC deposited
- Your wallet private key (see setup below)

---

## Installation

```bash
git clone <repo-url>
cd hl-trade

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

---

## Configuration

Hyperliquid has **no API keys** — authentication is Ethereum wallet signing. There are two ways to configure this:

### Option A — Direct wallet (simplest, good for testnet)

Use the private key of the wallet that holds your USDC deposits.

```bash
cp .env.example .env
```

Edit `.env`:

```env
PRIVATE_KEY=0x_your_main_wallet_private_key
NETWORK=testnet   # switch to mainnet when ready
```

**How to find your private key:**
- MetaMask → three-dot menu → Account Details → Export Private Key
- Hyperliquid native wallet → Settings → Export Key

### Option B — Agent wallet (recommended for mainnet)

An agent wallet is a separate Ethereum wallet you authorize to trade on behalf of your main account. It **cannot withdraw funds**, so a leaked key cannot drain your account.

**Setup:**

1. Generate a fresh wallet:
   ```bash
   python -c "from eth_account import Account; import secrets; a = Account.from_key(secrets.token_hex(32)); print('Private key:', a.key.hex()); print('Address:', a.address)"
   ```

2. Approve it on Hyperliquid:
   - Go to `https://app.hyperliquid.xyz` (or testnet equivalent)
   - Settings → API → Add API Wallet → paste the address from step 1

3. Edit `.env`:
   ```env
   PRIVATE_KEY=0x_agent_wallet_private_key
   ACCOUNT_ADDRESS=0x_your_main_account_address
   NETWORK=mainnet
   ```

> **Security** — never share or commit your `.env` file. It is already listed in `.gitignore`.

---

## Commands

All commands are run as:

```bash
python main.py <command> [options]
```

Pass `--help` to any command for a full option list.

---

### `open` — Open a position

```bash
# Market long, 10x leverage
python main.py open --coin BTC --side long --size 0.01 --leverage 10

# Limit short at a specific price
python main.py open --coin ETH --side short --size 0.5 --leverage 5 --limit 3800

# Market long with take-profit and stop-loss set automatically
python main.py open --coin BTC --side long --size 0.01 --leverage 10 \
  --tp 120000 --sl 95000

# Isolated margin instead of cross
python main.py open --coin SOL --side long --size 10 --leverage 3 --isolated
```

| Option | Description |
|---|---|
| `--coin` | Asset symbol, e.g. `BTC`, `ETH`, `SOL` |
| `--side` | `long` or `short` |
| `--size` | Position size in base asset units |
| `--leverage` | Leverage multiplier (default: 1) |
| `--limit` | Limit price — omit for a market order |
| `--cross / --isolated` | Margin mode (default: cross) |
| `--tp` | Take-profit trigger price |
| `--sl` | Stop-loss trigger price |

> **Market orders** are simulated with an IOC limit at ±2% from the current mid price. The order fills immediately or is cancelled — no partial resting.

---

### `close` — Close a position

Hyperliquid keeps **one position per coin**. Multiple entries at different prices are merged into a single position with an average entry price. Closing targets that aggregated position.

```bash
# Close the full BTC position at market
python main.py close --coin BTC

# Partial close (0.005 BTC)
python main.py close --coin BTC --size 0.005

# Limit close at a specific price
python main.py close --coin BTC --limit 105000
```

| Option | Description |
|---|---|
| `--coin` | Asset symbol |
| `--size` | Amount to close — omit to close the entire position |
| `--limit` | Limit price — omit for market |

---

### `tp` — Set a take-profit order

Places a reduce-only trigger order that executes at market when the price reaches your target.

```bash
# TP on your full BTC position
python main.py tp --coin BTC --price 120000

# TP on a specific size only
python main.py tp --coin BTC --price 120000 --size 0.005
```

---

### `sl` — Set a stop-loss order

```bash
# SL on your full BTC position
python main.py sl --coin BTC --price 95000

# SL on a specific size
python main.py sl --coin BTC --price 95000 --size 0.005
```

> TP and SL are independent reduce-only orders on the book. If you partially close a position manually, the trigger orders remain at their original size — use `cancel` then re-set them to match your remaining position.

---

### `cancel` — Cancel TP or SL orders

```bash
python main.py cancel --coin BTC --type tp
python main.py cancel --coin BTC --type sl
```

---

### `positions` — View open positions

```bash
python main.py positions
```

Shows a table with: coin, side, size, average entry price, current mark price, liquidation price, unrealized PnL, and leverage. Followed by account value, margin used, and withdrawable balance.

---

### `orders` — View open orders

```bash
python main.py orders
```

Lists all resting orders including limit orders and any active TP/SL triggers.

---

### `price` — Current mid price

```bash
python main.py price BTC
python main.py price ETH
```

---

### `assets` — List tradeable perpetuals

```bash
python main.py assets
```

---

## Testing on Testnet

Testnet lets you trade with mock USDC at no real cost. All commands work identically.

**1. Set up your `.env` for testnet:**

```env
PRIVATE_KEY=0x_your_wallet_private_key
NETWORK=testnet
```

**2. Get testnet funds:**

Visit the faucet and claim 1,000 mock USDC:
```
https://app.hyperliquid-testnet.xyz/drip
```

> **Note:** The faucet requires that the same wallet address has made at least one deposit on mainnet previously. If you are using a fresh wallet, make a small deposit on mainnet first (any amount).

**3. Deposit into the testnet trading account:**

Go to `https://app.hyperliquid-testnet.xyz` in your browser, connect your wallet, and deposit the mock USDC into your perp account.

**4. Run through the full flow:**

```bash
# Check available markets
python main.py assets

# Check your account
python main.py positions

# Open a small long
python main.py open --coin BTC --side long --size 0.001 --leverage 5

# Verify it appeared
python main.py positions

# Set a TP and SL
python main.py tp --coin BTC --price 200000
python main.py sl --coin BTC --price 50000

# Confirm the trigger orders are live
python main.py orders

# Close the position
python main.py close --coin BTC

# Verify it's gone
python main.py positions
```

This covers the complete lifecycle. Switch `NETWORK=mainnet` in `.env` when you are ready to trade with real funds.

---

## Notes

- **Fees** — Hyperliquid charges taker fees on market/IOC fills and maker rebates on resting limit orders.
- **Funding rate** — Perpetuals charge a funding rate periodically. Check the Hyperliquid UI for current rates.
- **Liquidation** — Keep an eye on the liquidation price shown in `positions`. Margin can be added via the Hyperliquid web app.
