# Setup Guide

This guide covers everything you need to do once before you can start trading.

---

## 1. Requirements

- Python 3.10 or newer
- A Hyperliquid account with USDC deposited
- Git

---

## 2. Install

```bash
git clone https://github.com/whynotvlad/hl-trade.git
cd hl-trade

python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

---

## 3. Configure credentials

Hyperliquid does not use API keys. Authentication is Ethereum wallet signing.
The recommended approach is an **agent wallet** — a separate key that can trade
but cannot withdraw funds. If a key leaks, your deposits are safe.

### Step 1 — Generate an agent wallet

Run this once inside your project virtualenv:

```bash
python -c "
from eth_account import Account
import secrets
a = Account.from_key(secrets.token_hex(32))
print('Private key:', a.key.hex())
print('Address:    ', a.address)
"
```

You will see output like:

```
Private key: 0xabc123...
Address:     0xDef456...
```

**Save the private key** in a password manager. You will not see it again.

### Step 2 — Approve the agent wallet on Hyperliquid

1. Go to [app.hyperliquid.xyz](https://app.hyperliquid.xyz) and log in
2. Open **Settings → API**
3. Click **Add API Wallet** and paste the **Address** from Step 1
4. Sign the transaction your wallet prompts — this registers the agent on-chain

For testnet, do the same on [app.hyperliquid-testnet.xyz](https://app.hyperliquid-testnet.xyz).

### Step 3 — Create your .env file

```bash
cp .env.example .env
```

Open `.env` and fill in:

```env
PRIVATE_KEY=0x_agent_wallet_private_key_from_step_1
ACCOUNT_ADDRESS=0x_your_main_hyperliquid_address
NETWORK=testnet   # change to mainnet when ready
```

`ACCOUNT_ADDRESS` is the address you log into Hyperliquid with — visible in the
top-right corner of the app.

> **Security** — `.env` is in `.gitignore` and will never be committed.
> Never share it or store it in plaintext outside a password manager.

---

## 4. Test your setup

```bash
python main.py positions
```

Expected output (no open positions):

```
No open positions.

  Perp account:  $0.00
  Spot USDC:     $999.00
  Total:         $999.00
```

If you see your balance, everything is working.

---

## 5. Get testnet funds (testnet only)

Visit the faucet to claim 1,000 mock USDC:

```
https://app.hyperliquid-testnet.xyz/drip
```

> The faucet requires at least one prior mainnet deposit from the same address.

---

## 6. Switch to mainnet

### Approve the agent wallet on mainnet

The agent wallet approval is per-network. If you approved it on testnet, you
must also approve it on mainnet before trading with real funds:

1. Go to [app.hyperliquid.xyz](https://app.hyperliquid.xyz) and log in
2. **Settings → API → Add API Wallet**
3. Paste the same agent wallet address you generated in Step 1
4. Sign the transaction

### Set mainnet as default

Edit `.env`:

```env
NETWORK=mainnet
```

Or keep `NETWORK=testnet` as the default and use `--network mainnet` on
individual commands to trade on mainnet explicitly:

```bash
python main.py --network mainnet positions
python main.py --network mainnet open --coin BTC --side long --size 0.01 --leverage 5
```

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `PRIVATE_KEY is not set` | Missing `.env` file | `cp .env.example .env` and fill it in |
| `User or API Wallet does not exist` | Agent wallet not approved | Complete Step 2 above — you must sign the approval transaction |
| `Order has invalid price` | Price formatting issue | Use `python main.py price BTC` to check current prices |
| `No open position for X` | Tried to close/TP/SL with no position | Open a position first with `open` |
| Balance shows `$0.00` | USDC is in spot wallet, not perp account | Normal — `positions` shows both |
