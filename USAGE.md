# CLI Usage Guide

All commands are run from the project directory with the virtualenv active:

```bash
source .venv/bin/activate
python main.py <command> [options]
```

Add `--help` to any command to see all options:

```bash
python main.py open --help
```

---

## Commands at a glance

| Command | What it does |
|---|---|
| `open` | Open a long or short position |
| `close` | Close a position fully or partially |
| `tp` | Set a take-profit trigger order |
| `sl` | Set a stop-loss trigger order |
| `cancel` | Cancel a TP, SL, or any order by ID |
| `positions` | Show open positions and account balance |
| `orders` | Show all open orders |
| `price` | Show current mid price for an asset |
| `assets` | List all tradeable perpetuals |

---

## `open` — Open a position

### Options

| Option | Required | Description |
|---|---|---|
| `--coin` | Yes | Asset symbol, e.g. `BTC`, `ETH`, `SOL` |
| `--side` | Yes | `long` or `short` |
| `--size` | Yes | Size in base asset units (e.g. `0.01` BTC) |
| `--leverage` | No | Leverage multiplier, default `1` |
| `--limit` | No | Limit price — omit for a market order |
| `--cross / --isolated` | No | Margin mode, default cross |
| `--tp` | No | Take-profit trigger price |
| `--sl` | No | Stop-loss trigger price |

### Examples

```bash
# Market long, 10x leverage
python main.py open --coin BTC --side long --size 0.01 --leverage 10

# Market short
python main.py open --coin ETH --side short --size 0.5 --leverage 5

# Limit long — rests on the book until filled
python main.py open --coin BTC --side long --size 0.01 --leverage 10 --limit 58000

# Open with take-profit and stop-loss in one command
python main.py open --coin BTC --side long --size 0.01 --leverage 10 \
  --tp 120000 --sl 95000

# Isolated margin
python main.py open --coin SOL --side long --size 10 --leverage 5 --isolated
```

> **Market orders** are simulated with an IOC limit at ±2% from the current
> mid price. The order fills immediately or is cancelled entirely — no partial
> resting on the book.

> **Minimum order value** is $10 notional. For low-price assets increase size
> accordingly (e.g. `--size 5` for a $2 coin at 1x leverage).

---

## `close` — Close a position

Hyperliquid keeps **one position per coin**. Multiple entries at different prices
are merged into a single position with an averaged entry price. Closing targets
that aggregated position.

### Options

| Option | Required | Description |
|---|---|---|
| `--coin` | Yes | Asset symbol |
| `--size` | No | Amount to close — omit to close the full position |
| `--limit` | No | Limit price — omit for market |

### Examples

```bash
# Close full BTC position at market
python main.py close --coin BTC

# Close half the position
python main.py close --coin BTC --size 0.005

# Limit close — rests as a reduce-only order until filled
python main.py close --coin ETH --limit 3200
```

---

## `tp` — Set a take-profit order

Places a reduce-only trigger that executes at market when the mark price reaches
your target. For a long, set `--price` above entry. For a short, set it below.

### Options

| Option | Required | Description |
|---|---|---|
| `--coin` | Yes | Asset symbol |
| `--price` | Yes | Trigger price |
| `--size` | No | Size to close at trigger — omit to use full position size |

### Examples

```bash
# TP on full BTC long position
python main.py tp --coin BTC --price 120000

# TP on a specific size only (partial TP)
python main.py tp --coin BTC --price 120000 --size 0.005

# TP on ETH short (price below entry)
python main.py tp --coin ETH --price 1200
```

---

## `sl` — Set a stop-loss order

Same mechanics as `tp` but uses the `sl` trigger type. For a long, set
`--price` below entry. For a short, set it above.

### Examples

```bash
# SL on full BTC long
python main.py sl --coin BTC --price 95000

# SL on ETH short (price above entry)
python main.py sl --coin ETH --price 1800

# Partial SL
python main.py sl --coin BTC --price 95000 --size 0.005
```

> **Important:** TP and SL are independent reduce-only orders on the book.
> If you partially close a position manually, the trigger orders remain at
> their original size. Use `cancel` to remove them and re-set with the
> correct size.

---

## `cancel` — Cancel orders

### Options

| Option | Required | Description |
|---|---|---|
| `--coin` | Yes | Asset symbol |
| `--type` | One of these | `tp` or `sl` — cancels all matching orders for the coin |
| `--id` | One of these | Specific order ID from `orders` — cancels any order type |

### Examples

```bash
# Cancel all TP orders for BTC
python main.py cancel --coin BTC --type tp

# Cancel all SL orders for ETH
python main.py cancel --coin ETH --type sl

# Cancel a specific order by ID (works for limit orders too)
python main.py cancel --coin BTC --id 54479543383
```

---

## `positions` — View open positions

```bash
python main.py positions
```

Shows a table with every open position, followed by the account summary:

```
                         Open Positions
┌──────┬───────┬───────┬────────────┬────────────┬───────────┬────────────┬──────────┐
│ Coin │ Side  │ Size  │ Entry Price│ Mark Price │ Liq. Price│ Unreal. PnL│ Leverage │
├──────┼───────┼───────┼────────────┼────────────┼───────────┼────────────┼──────────┤
│ BTC  │ LONG  │ 0.01  │ $62,000.00 │ $62,500.00 │     -     │   +$5.00   │ 10x cross│
│ ETH  │ SHORT │ 0.5   │  $1,560.00 │  $1,545.00 │$11,000.00 │   +$7.50   │ 5x cross │
└──────┴───────┴───────┴────────────┴────────────┴───────────┴────────────┴──────────┘

  Perp account:  $650.00
  Spot USDC:     $350.00
  Total:         $1,000.00
  Margin used:   $124.00
  Withdrawable:  $526.00
```

**Liq. Price** is only shown for isolated margin positions.
**Perp account** is the active collateral backing open positions.
**Spot USDC** is the balance available in your wallet (shown separately on the unified account).

---

## `orders` — View open orders

```bash
python main.py orders
```

Lists all resting orders — limit orders, take-profit triggers, and stop-loss
triggers — with their order IDs (needed for `cancel --id`).

---

## `price` — Current mid price

```bash
python main.py price BTC
python main.py price ETH
python main.py price SOL
```

---

## `assets` — List tradeable perpetuals

```bash
python main.py assets
```

Shows all available coins with their maximum leverage. Use these symbols with
`--coin` in all other commands.

---

## Typical trading flows

### Open and manage a position

```bash
# 1. Check price
python main.py price BTC

# 2. Open long with protection
python main.py open --coin BTC --side long --size 0.01 --leverage 10 \
  --tp 120000 --sl 95000

# 3. Monitor
python main.py positions
python main.py orders

# 4. Close when ready
python main.py close --coin BTC
```

### Adjust TP/SL after opening

```bash
# Cancel existing and re-set
python main.py cancel --coin BTC --type tp
python main.py tp --coin BTC --price 125000

python main.py cancel --coin BTC --type sl
python main.py sl --coin BTC --price 98000
```

### Partial take-profit

```bash
# Close half at market now, let the rest run with a TP
python main.py close --coin BTC --size 0.005
python main.py cancel --coin BTC --type tp
python main.py tp --coin BTC --price 130000 --size 0.005
```

### Scale into a position

```bash
# First entry
python main.py open --coin ETH --side long --size 0.5 --leverage 5

# Add to the position — Hyperliquid merges it automatically at a new average
python main.py open --coin ETH --side long --size 0.5 --leverage 5

# Positions shows one combined ETH long with averaged entry price
python main.py positions
```
