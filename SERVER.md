# Server Deployment

How the bot runs in production on the Ubuntu VPS.

---

## Overview

| Component | Where |
|---|---|
| **Bot process** | Ubuntu VPS — runs as a `systemd` service |
| **HTML forms** | GitHub Pages — `https://whynotvlad.github.io/hl-trade/` |
| **Code repo** | `https://github.com/whynotvlad/hl-trade` |
| **Database** | `/home/ubuntu/hl-trade/users.db` (SQLite, encrypted fields) |

---

## SSH Access

```bash
ssh ubuntu@<server-ip>
# or if you use a key alias in ~/.ssh/config:
ssh hl-trade
```

Recommended `~/.ssh/config` entry (add to your local machine):

```
Host hl-trade
    HostName <server-ip>
    User ubuntu
    IdentityFile ~/.ssh/id_ed25519
```

---

## First-time Server Setup

Run these once after provisioning a fresh Ubuntu 22.04 instance:

```bash
# Update and install Python
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git

# Clone the repo
cd ~
git clone https://github.com/whynotvlad/hl-trade.git
cd hl-trade

# Create virtualenv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Create .env from template
cp .env.example .env
nano .env   # fill in TELEGRAM_TOKEN, PRIVATE_KEY, ACCOUNT_ADDRESS, NETWORK, etc.
```

---

## systemd Service

Create the service file on the server:

```bash
sudo nano /etc/systemd/system/hl-bot.service
```

Paste:

```ini
[Unit]
Description=HL Trade Telegram Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/hl-trade
EnvironmentFile=/home/ubuntu/hl-trade/.env
ExecStart=/home/ubuntu/hl-trade/.venv/bin/python bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable hl-bot
sudo systemctl start hl-bot
```

### Useful service commands

```bash
sudo systemctl status hl-bot        # current state
sudo systemctl restart hl-bot       # restart after code changes
sudo systemctl stop hl-bot          # stop
sudo journalctl -u hl-bot -f        # stream live logs
sudo journalctl -u hl-bot -n 100    # last 100 log lines
```

---

## Deploying Updates

Whenever you push code changes to GitHub, deploy to the server:

```bash
ssh hl-trade
cd hl-trade
git pull
sudo systemctl restart hl-bot
sudo journalctl -u hl-bot -f        # verify it started cleanly
```

One-liner (run from your local machine):

```bash
ssh hl-trade "cd hl-trade && git pull && sudo systemctl restart hl-bot"
```

---

## Deploying HTML Forms (GitHub Pages)

The two Web App forms (`open.html`, `ladder.html`) are served directly from GitHub Pages — no server-side work needed. They update automatically when you push to the `main` branch.

```bash
# From your local machine
git add open.html ladder.html
git commit -m "update forms"
git push
# GitHub Pages deploys within ~30 seconds
```

URLs:
- `https://whynotvlad.github.io/hl-trade/open.html?v=7`
- `https://whynotvlad.github.io/hl-trade/ladder.html?v=1`

The `?v=N` query string busts the Telegram WebApp cache. Increment `v` in `bot.py` constants (`WEB_APP_URL`, `LADDER_FORM_URL`) and in the HTML `<title>` comment whenever you make a breaking change to the form.

---

## Environment Variables

All secrets live in `/home/ubuntu/hl-trade/.env` on the server (never committed to git).

| Variable | Purpose |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from @BotFather |
| `ADMIN_IDS` | Comma-separated Telegram user IDs that always have access |
| `PRIVATE_KEY` | Agent wallet private key (`0x…`) |
| `ACCOUNT_ADDRESS` | Master Hyperliquid account address (`0x…`) |
| `NETWORK` | `mainnet` or `testnet` |
| `DB_ENCRYPTION_KEY` | Fernet key for encrypting agent keys in `users.db` |

Generate a fresh `DB_ENCRYPTION_KEY`:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## Database

`users.db` is a SQLite file stored in the repo directory on the server. It holds:

| Table | Contents |
|---|---|
| `users` | Registered users — encrypted agent private key per user |
| `allowed_users` | Whitelist of Telegram IDs allowed to use the bot |
| `alerts` | Active price alerts |

The file is excluded from git (`.gitignore`). Back it up separately if needed:

```bash
# From local machine
scp hl-trade:/home/ubuntu/hl-trade/users.db ./users.db.bak
```

---

## Logs

The bot writes to stdout/stderr; `journalctl` captures everything via systemd:

```bash
# Live tail
sudo journalctl -u hl-bot -f

# Last 200 lines
sudo journalctl -u hl-bot -n 200

# Since a specific time
sudo journalctl -u hl-bot --since "1 hour ago"
```
