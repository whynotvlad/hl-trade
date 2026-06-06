import datetime
import fcntl
import logging
import os
import sys
import time
from typing import Optional

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import db
from client import HLClient

load_dotenv()

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)

# Single-instance guard: exit immediately if another bot.py is already running.
_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot.lock")
_lock_fh = open(_LOCK_FILE, "w")
try:
    fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    logging.error("Another bot instance is already running — exiting.")
    sys.exit(77)  # 77 = already running; service file suppresses restart on this code

TOKEN = os.getenv("TELEGRAM_TOKEN")

# Admins: IDs in ALLOWED_TG_IDS env var. They always have access and can /adduser others.
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_TG_IDS", "").split(",")
    if x.strip()
}

_clients: dict[tuple, HLClient] = {}
_pending: dict[int, dict] = {}            # tg_id -> {fn, preview, expires}
_snapshots: dict[int, dict] = {}          # tg_id -> {order_ids, orders, positions}
_liq_warned: dict[str, bool] = {}         # "{tg_id}_{coin}" -> True when warning already sent
_awaiting_partial: dict[int, str] = {}              # tg_id -> coin, waiting for partial close size
_awaiting_price: dict[int, tuple[str, str]] = {}    # tg_id -> (action, coin), waiting for TP/SL price
_price_history: dict[str, list] = {}      # coin -> [(ts, price), ...] rolling 25h window
_move_alerted: dict[str, float] = {}      # "{tg_id}_{coin}_{tier}" -> last alert ts
_poll_errors: dict[int, int] = {}         # tg_id -> consecutive error count
_poll_error_alerted: set = set()          # tg_ids already notified this error streak
_last_fill_ts: dict[int, float] = {}      # tg_id -> ms timestamp of newest fill already notified
PRICE_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_history.json")

CONFIRM_TTL = 60
POLL_INTERVAL = 30
LIQ_WARN_PCT = 15
DIGEST_TIME = datetime.time(hour=0, minute=0, tzinfo=datetime.timezone.utc)  # midnight UTC
MOVE_1H_PCT  = 3.0    # % move in 1 hour to trigger quick-move alert
MOVE_24H_PCT = 8.0    # % move in 24 hours to trigger big-move alert
MOVE_COOLDOWN = 2 * 3600  # seconds before re-alerting same coin+tier
WEB_APP_URL    = "https://whynotvlad.github.io/hl-trade/open.html?v=10"
LADDER_FORM_URL = "https://whynotvlad.github.io/hl-trade/ladder.html?v=2"

QUICK_KEYS = ReplyKeyboardMarkup(
    [
        ["/positions", "/orders"],
        ["/pnl",       "/risk"],
        ["/chart",     "/price BTC"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# ── help texts ────────────────────────────────────────────────────────────────

_HELP: dict[str, str] = {
    "register": (
        "Link your Hyperliquid account to the bot.\n\n"
        "Usage:\n"
        "  /register <agent_key> <account_address>\n\n"
        "Both values start with 0x.\n\n"
        "How to get them:\n"
        "1. Generate an agent wallet (see SETUP.md in the repo)\n"
        "2. Approve it at app.hyperliquid.xyz → Settings → API\n"
        "3. agent_key   = the agent wallet private key\n"
        "   account_address = your main Hyperliquid address\n\n"
        "WARNING: Delete the message after sending — it contains your key."
    ),
    "open": (
        "Open a long or short position.\n\n"
        "Usage:\n"
        "  /open <coin> <long|short> <size> <leverage> [tp] [sl]\n\n"
        "Examples:\n"
        "  /open BTC long 0.001 10\n"
        "  /open ETH short 0.1 5\n"
        "  /open BTC long 0.001 10 75000 55000\n\n"
        "coin     — symbol from /assets (BTC, ETH, SOL...)\n"
        "size     — amount in base units (0.001 BTC ~= $62)\n"
        "leverage — 1-50 depending on coin\n"
        "tp       — optional take-profit price\n"
        "sl       — optional stop-loss price"
    ),
    "close": (
        "Close an open position fully or partially.\n\n"
        "Usage:\n"
        "  /close <coin> [size]\n\n"
        "Examples:\n"
        "  /close BTC          — close the full position\n"
        "  /close BTC 0.0005   — close half of a 0.001 BTC position"
    ),
    "tp": (
        "Set a take-profit trigger on an open position.\n\n"
        "Usage:\n"
        "  /tp <coin> <price> [size]\n\n"
        "Examples:\n"
        "  /tp BTC 75000         — TP on full position\n"
        "  /tp BTC 75000 0.0005  — TP on partial size\n\n"
        "For a LONG set price above your entry.\n"
        "For a SHORT set price below your entry."
    ),
    "sl": (
        "Set a stop-loss trigger on an open position.\n\n"
        "Usage:\n"
        "  /sl <coin> <price> [size]\n\n"
        "Examples:\n"
        "  /sl BTC 55000         — SL on full position\n"
        "  /sl BTC 55000 0.0005  — SL on partial size"
    ),
    "cancel": (
        "Cancel a TP, SL, or any order by ID.\n\n"
        "Usage:\n"
        "  /cancel <coin> <tp|sl|order_id>\n\n"
        "Examples:\n"
        "  /cancel BTC tp          — cancel all BTC take-profit orders\n"
        "  /cancel BTC sl          — cancel all BTC stop-loss orders\n"
        "  /cancel BTC 54479543383 — cancel specific order by ID\n\n"
        "Get order IDs from /orders."
    ),
    "alert": (
        "Set a price alert for any coin.\n\n"
        "Usage:\n"
        "  /alert <coin> <price>\n\n"
        "Examples:\n"
        "  /alert BTC 70000\n"
        "  /alert ETH 2000\n\n"
        "The bot will message you when the price crosses your target.\n"
        "View active alerts: /alerts\n"
        "Cancel an alert:   /cancelalert <id>"
    ),
    "ladder": (
        "Close a position in evenly-spaced limit orders (scaled exit).\n\n"
        "Usage:\n"
        "  /ladder <coin> <parts> <from_price> <to_price>\n\n"
        "Examples:\n"
        "  /ladder ETH 5 3500 3000   — 5 orders, $3500 down to $3000\n"
        "  /ladder BTC 3 65000 63000 — 3 orders, $65000 down to $63000\n\n"
        "parts      — number of orders (2-20)\n"
        "from_price — price of the first order\n"
        "to_price   — price of the last order\n\n"
        "Each order is reduce-only (cannot increase your position).\n"
        "All orders placed as resting limit GTC — visible in /orders."
    ),
    "slladder": (
        "Close a position in evenly-spaced stop-loss trigger orders (scaled stop).\n\n"
        "Usage:\n"
        "  /slladder <coin> <parts> <from_price> <to_price>\n\n"
        "Examples:\n"
        "  /slladder ETH 5 2900 2600   — SHORT: 5 stop triggers from $2900 up to... wait\n"
        "  /slladder BTC 3 58000 55000 — LONG: 3 stop triggers at $58k, $56.5k, $55k\n\n"
        "Each trigger fires a market close for 1/N of your position.\n"
        "Use prices on the loss side of current market:\n"
        "  LONG  → prices below current price\n"
        "  SHORT → prices above current price\n\n"
        "Orders appear in /orders as stop triggers."
    ),
    "pnl": (
        "Show realised PnL for the last 7 days.\n\n"
        "Usage:\n"
        "  /pnl\n\n"
        "Includes per-coin breakdown and total fees paid."
    ),
    "risk": (
        "Show your current risk exposure.\n\n"
        "Usage:\n"
        "  /risk\n\n"
        "Shows total notional, margin usage, margin ratio,\n"
        "and distance to liquidation for each position."
    ),
}

# ── helpers ───────────────────────────────────────────────────────────────────

def _get_client(tg_id: int) -> HLClient:
    creds = db.get_user(tg_id)
    if not creds:
        raise ValueError(
            "You are not registered yet.\n"
            "Send /register <agent_key> <account_address>\n\n"
            "Need help? /help register"
        )
    key = (tg_id, os.getenv("NETWORK", "mainnet"))
    if key not in _clients:
        _clients[key] = HLClient(
            private_key=creds["agent_key"],
            account_address=creds["account_address"],
        )
    return _clients[key]


def _is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS


def _is_allowed(tg_id: int) -> bool:
    return _is_admin(tg_id) or db.is_allowed_user(tg_id)


async def _guard(update: Update) -> bool:
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("Access denied.")
        return False
    return True


async def _admin_guard(update: Update) -> bool:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return False
    return True


def _store_pending(tg_id: int, preview: str, fn):
    _pending[tg_id] = {"fn": fn, "preview": preview, "expires": time.time() + CONFIRM_TTL}


def _pop_pending(tg_id: int) -> Optional[dict]:
    entry = _pending.pop(tg_id, None)
    if entry and time.time() > entry["expires"]:
        return None
    return entry


def _fmt_result(result) -> str:
    # Ladder close returns a list of results
    if isinstance(result, list):
        ok     = sum(1 for r in result if r.get("status") == "ok")
        failed = len(result) - ok
        msg = f"Ladder: placed {ok}/{len(result)} orders."
        if failed:
            msg += f"\n{failed} failed — check /orders for what went through."
        return msg
    if result.get("status") != "ok":
        return f"Error: {result}"
    lines = []
    response_type = result.get("response", {}).get("type")
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    for s in statuses:
        if response_type == "cancel" or s == "success":
            lines.append("Cancelled.")
        elif isinstance(s, dict):
            if "filled" in s:
                f = s["filled"]
                lines.append(f"Filled\nAvg price: ${float(f.get('avgPx', 0)):,.2f}\nSize: {f.get('totalSz')}")
            elif "resting" in s:
                lines.append(f"Order placed (resting)\nID: {s['resting'].get('oid')}\n(check with /orders)")
            elif "error" in s:
                lines.append(f"Error: {s['error']}")
    return "\n".join(lines) or "Done."


def _fmt_positions(state: dict, prices: dict, spot_usdc: float) -> str:
    positions = [
        e for e in state.get("assetPositions", [])
        if float(e["position"]["szi"]) != 0
    ]
    lines = []
    if not positions:
        lines.append("No open positions.")
    else:
        for e in positions:
            pos = e["position"]
            size = float(pos["szi"])
            is_long = size > 0
            side = "LONG" if is_long else "SHORT"
            pnl = float(pos.get("unrealizedPnl", 0))
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            mark = prices.get(pos["coin"])
            mark_str = f"${float(mark):,.2f}" if mark else "—"
            liq = pos.get("liquidationPx")
            liq_line = f"\n   Liq:      ${float(liq):,.2f} ⚠️" if liq else ""
            lev = pos.get("leverage", {})
            lines.append(
                f"{'🟢' if is_long else '🔴'} {pos['coin']} {side}\n"
                f"   Size:     {abs(size)}\n"
                f"   Entry:    ${float(pos['entryPx']):,.2f}\n"
                f"   Mark:     {mark_str}\n"
                f"   PnL:      {pnl_str} {'📈' if pnl >= 0 else '📉'}\n"
                f"   Leverage: {lev.get('value')}x {lev.get('type')}"
                f"{liq_line}"
            )

    summary = state.get("marginSummary", {})
    perp = float(summary.get("accountValue", 0))
    lines.append(
        f"\n💰 Balance\n"
        f"   Perp: ${perp:,.2f}\n"
        f"   Spot: ${spot_usdc:,.2f}"
    )
    return "\n".join(lines)


def _fmt_orders(orders: list) -> str:
    if not orders:
        return "No open orders."
    lines = ["📋 Open Orders\n"]
    for o in orders:
        side = "BUY" if o.get("side") == "B" else "SELL"
        lines.append(
            f"{'🟢' if side == 'BUY' else '🔴'} {o.get('coin')} {side}\n"
            f"   Type:  {o.get('orderType')}\n"
            f"   Size:  {o.get('sz')}\n"
            f"   Price: ${float(o.get('limitPx', 0)):,.2f}"
        )
    return "\n".join(lines)


def _order_tag(order: dict) -> str:
    """Return a compact one-line label for an order."""
    otype = order.get("orderType", "")
    side  = "B" if order.get("side") == "B" else "S"
    px    = float(order.get("limitPx", 0))
    sz    = order.get("sz", "?")
    oid   = order.get("oid", "")
    if "Take Profit" in otype:
        return f"   🎯 TP @ ${px:,.2f}  ({sz})  [oid:{oid}]"
    if "Stop" in otype:
        return f"   🛑 SL @ ${px:,.2f}  ({sz})  [oid:{oid}]"
    side_lbl = "Buy" if side == "B" else "Sell"
    return f"   📌 {side_lbl} limit @ ${px:,.2f}  ({sz})  [oid:{oid}]"


def _position_text_with_orders(pos: dict, prices: dict, orders: list) -> str:
    """Position card that also shows its attached TP/SL and any entry orders."""
    size    = float(pos["szi"])
    is_long = size > 0
    pnl     = float(pos.get("unrealizedPnl", 0))
    pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
    mark    = prices.get(pos["coin"])
    mark_str = f"${float(mark):,.2f}" if mark else "—"
    lev     = pos.get("leverage", {})
    liq     = pos.get("liquidationPx")
    liq_line = f"\n   Liq:     ${float(liq):,.2f} ⚠️" if liq else ""

    lines = [
        f"{'🟢' if is_long else '🔴'} {pos['coin']} {'LONG' if is_long else 'SHORT'}  "
        f"{lev.get('value')}x {lev.get('type', '')}",
        f"   Size:    {abs(size)}",
        f"   Entry:   ${float(pos['entryPx']):,.2f}   Mark: {mark_str}",
        f"   PnL:     {pnl_str} {'📈' if pnl >= 0 else '📉'}"
        f"{liq_line}",
    ]
    if orders:
        lines.append("   ─")
        for o in orders:
            lines.append(_order_tag(o))
    return "\n".join(lines)


# ── commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    name = update.effective_user.first_name or "there"
    registered = db.is_registered(update.effective_user.id)
    if registered:
        await update.message.reply_text(
            f"👋 Welcome back, {name}!\n\n"
            "/book — positions, orders & balance\n"
            "/price BTC — current price\n"
            "/pnl — 7-day realised PnL\n"
            "/stats — win rate & analytics\n"
            "/risk — margin & liquidation risk\n\n"
            "Need help? /help or /help <command>",
            reply_markup=QUICK_KEYS,
        )
    else:
        await update.message.reply_text(
            f"👋 Hey {name}! Welcome to hl-trade bot.\n\n"
            "This bot lets you trade Hyperliquid perpetuals from your phone.\n\n"
            "Setup (one time):\n"
            "1. Generate an agent wallet — it can trade but cannot withdraw funds\n"
            "2. Approve it at app.hyperliquid.xyz → Settings → API\n"
            "3. Send /register with your keys\n\n"
            "Full setup guide: /help register"
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if context.args:
        cmd = context.args[0].lower().lstrip("/")
        text = _HELP.get(cmd)
        if text:
            await update.message.reply_text(f"/{cmd}\n\n{text}")
        else:
            await update.message.reply_text(
                f"No help for '{cmd}'.\n\nAvailable: " + ", ".join(f"/{k}" for k in _HELP)
            )
    else:
        is_admin = _is_admin(update.effective_user.id)
        lines = [
            "Commands\n",
            "Account",
            "/book — positions, orders & balance (full book)",
            "/positions — alias for /book",
            "/pnl — 7-day realised PnL",
            "/risk — margin & liquidation risk",
            "/chart <coin> [5m|15m|1h|4h|1d] — candlestick chart",
            "/price <coin> — current price",
            "/assets — available markets",
            "",
            "Alerts",
            "/alert <coin> <price> — set price alert",
            "/alerts — list active alerts",
            "/cancelalert <id> — remove an alert",
            "",
            "Trading",
            "/open <coin> <long|short> <size> <leverage> [tp] [sl]",
            "/close <coin> [size]",
            "/ladder <coin> <parts> <from> <to>",
            "/slladder <coin> <parts> <from> <to>",
            "/tp <coin> <price> [size]",
            "/sl <coin> <price> [size]",
            "/cancel <coin> <tp|sl|order_id>",
            "",
            "Confirm / dismiss a pending trade",
            "/confirm — execute the previewed order",
            "/dismiss — discard it",
        ]
        if is_admin:
            lines += [
                "",
                "Admin",
                "/adduser <tg_id> — whitelist a new user",
                "/removeuser <tg_id> — revoke access",
                "/listusers — show all whitelisted users",
            ]
        lines.append("\nType /help <command> for detailed help.")
        await update.message.reply_text("\n".join(lines))


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "Usage: /register <agent_key> <account_address>\n\n"
            "Both start with 0x.\n\n"
            "/help register for full instructions."
        )
        return
    agent_key, account_address = args
    if not agent_key.startswith("0x") or not account_address.startswith("0x"):
        await update.message.reply_text("Both values must start with 0x.")
        return
    try:
        db.register_user(update.effective_user.id, agent_key, account_address)
        _clients.pop((update.effective_user.id, os.getenv("NETWORK", "mainnet")), None)
        _snapshots.pop(update.effective_user.id, None)
        await update.message.reply_text(
            "Registered!\n\n"
            "⚠️ Delete your /register message now — it contains your private key.\n\n"
            "Try /positions to verify the connection.",
            reply_markup=QUICK_KEYS,
        )
    except Exception as e:
        await update.message.reply_text(f"Registration failed: {e}")


async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    entry = _pop_pending(update.effective_user.id)
    if not entry:
        await update.message.reply_text(
            "Nothing to confirm — or the preview expired (60s).\n\n"
            "Use /open, /close, /tp, or /sl to create a new order."
        )
        return
    try:
        result = entry["fn"]()
        await update.message.reply_text(_fmt_result(result))
        await cmd_book(update, context)
    except Exception as e:
        await update.message.reply_text(f"Order failed: {e}")


async def cmd_dismiss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if _pending.pop(update.effective_user.id, None):
        await update.message.reply_text("Dismissed.")
    else:
        await update.message.reply_text("Nothing to dismiss.")


def _position_text(pos: dict, prices: dict) -> str:
    size = float(pos["szi"])
    is_long = size > 0
    side = "LONG" if is_long else "SHORT"
    pnl = float(pos.get("unrealizedPnl", 0))
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    mark = prices.get(pos["coin"])
    mark_str = f"${float(mark):,.2f}" if mark else "—"
    lev = pos.get("leverage", {})
    liq = pos.get("liquidationPx")
    liq_line = f"\n   Liq:      ${float(liq):,.2f} ⚠️" if liq else ""
    return (
        f"{'🟢' if is_long else '🔴'} {pos['coin']} {side}\n"
        f"   Size:     {abs(size)}\n"
        f"   Entry:    ${float(pos['entryPx']):,.2f}\n"
        f"   Mark:     {mark_str}\n"
        f"   PnL:      {pnl_str} {'📈' if pnl >= 0 else '📉'}\n"
        f"   Leverage: {lev.get('value')}x {lev.get('type')}"
        f"{liq_line}"
    )


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for /book — kept for backwards compatibility."""
    await cmd_book(update, context)


async def handle_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_id = query.from_user.id
    if not _is_allowed(tg_id):
        return

    parts = query.data.split(":", 2)
    action = parts[0]
    coin   = parts[1] if len(parts) > 1 else ""
    extra  = parts[2] if len(parts) > 2 else ""

    try:
        client = _get_client(tg_id)
        pos = client._find_position(coin)
        if not pos:
            await query.edit_message_text(f"No open {coin} position.")
            return

        pos_size = float(pos["szi"])
        is_long  = pos_size > 0
        price    = client.get_mid_price(coin)

        # ── close_pct: 25 / 50 / 100 — executes immediately, no /confirm ─────
        if action == "close_pct":
            pct        = int(extra)
            close_size = round(abs(pos_size) * pct / 100, 8)
            pnl        = (price - float(pos["entryPx"])) * close_size * (1 if is_long else -1)
            pnl_str    = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            label      = "full" if pct == 100 else f"{pct}%"

            await context.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"Closing {label} of {coin} {'LONG' if is_long else 'SHORT'} "
                    f"({close_size} @ ~${price:,.2f})…"
                ),
            )
            client.close_position(coin=coin, size=None if pct == 100 else close_size)
            await context.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"✅ Closed {label} of {coin}\n"
                    f"   Price:   ~${price:,.2f}\n"
                    f"   Est PnL: {pnl_str} {'📈' if pnl >= 0 else '📉'}"
                ),
            )

        elif action == "close_partial":
            _awaiting_partial[tg_id] = coin
            await context.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"How much {coin} to close?\n"
                    f"Max: {abs(pos_size)} {coin}\n\n"
                    f"Type the amount:"
                ),
            )

        elif action == "set_tp":
            _awaiting_price[tg_id] = ("tp", coin)
            direction = "above" if is_long else "below"
            await context.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"Set Take Profit for {coin} {'LONG' if is_long else 'SHORT'}\n"
                    f"Current price: ~${price:,.2f}\n"
                    f"TP should be {direction} current price.\n\n"
                    f"Type the TP price $:"
                ),
            )

        elif action == "set_sl":
            _awaiting_price[tg_id] = ("sl", coin)
            direction = "below" if is_long else "above"
            await context.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"Set Stop Loss for {coin} {'LONG' if is_long else 'SHORT'}\n"
                    f"Current price: ~${price:,.2f}\n"
                    f"SL should be {direction} current price.\n\n"
                    f"Type the SL price $:"
                ),
            )

    except Exception as e:
        await context.bot.send_message(chat_id=tg_id, text=f"Error: {e}")


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not _is_allowed(tg_id):
        return

    # TP/SL price input takes priority
    price_action = _awaiting_price.pop(tg_id, None)
    if price_action:
        action, coin = price_action
        try:
            trigger_px = float(update.message.text.strip())
            if trigger_px <= 0:
                await update.message.reply_text("Price must be greater than 0. Try again: /positions")
                return
            client = _get_client(tg_id)
            pos = client._find_position(coin)
            if not pos:
                await update.message.reply_text(f"No open {coin} position.")
                return
            is_long = float(pos["szi"]) > 0
            label = "Take Profit" if action == "tp" else "Stop Loss"
            emoji = "🎯" if action == "tp" else "🛑"
            preview = (
                f"Order Preview\n\n"
                f"{emoji} {label} for {coin} {'LONG' if is_long else 'SHORT'}\n"
                f"   Trigger: ${trigger_px:,.2f}\n\n"
                f"Send /confirm to place or /dismiss to cancel.\n"
                f"Expires in {CONFIRM_TTL}s."
            )
            fn = (
                (lambda c=coin, p=trigger_px: client.set_tp(c, p))
                if action == "tp"
                else (lambda c=coin, p=trigger_px: client.set_sl(c, p))
            )
            _store_pending(tg_id, preview, fn)
            await update.message.reply_text(preview)
        except ValueError:
            await update.message.reply_text("Please enter a valid price, e.g. 65000")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")
        return

    # Partial close size input
    coin = _awaiting_partial.pop(tg_id, None)
    if not coin:
        return  # not waiting for anything — ignore plain text
    try:
        size = float(update.message.text.strip())
        if size <= 0:
            await update.message.reply_text("Size must be greater than 0. Try again: /positions")
            return
        client = _get_client(tg_id)
        pos = client._find_position(coin)
        if not pos:
            await update.message.reply_text(f"No open position for {coin}.")
            return
        pos_size = float(pos["szi"])
        max_size = abs(pos_size)
        if size > max_size:
            await update.message.reply_text(
                f"Size {size} exceeds position size {max_size}.\n"
                f"Try again: /positions"
            )
            return
        is_long = pos_size > 0
        price = client.get_mid_price(coin)
        pnl = (price - float(pos["entryPx"])) * size * (1 if is_long else -1)
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        preview = (
            f"Order Preview\n\n"
            f"{'🟢' if is_long else '🔴'} Close {coin} {'LONG' if is_long else 'SHORT'}\n"
            f"   Size:    {size} (partial)\n"
            f"   Price:   ~${price:,.2f}\n"
            f"   Est PnL: {pnl_str} {'📈' if pnl >= 0 else '📉'}\n\n"
            f"Send /confirm to execute or /dismiss to cancel.\n"
            f"Expires in {CONFIRM_TTL}s."
        )
        _store_pending(
            tg_id, preview,
            lambda c=coin, s=size: client.close_position(coin=c, size=s),
        )
        await update.message.reply_text(preview)
    except ValueError:
        await update.message.reply_text("Please enter a valid number, e.g. 0.0005")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            "Tap the button below to open the trade form:",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("Open Trade Form", web_app=WebAppInfo(url=WEB_APP_URL))]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )
        return
    try:
        coin = args[0].upper()
        side = args[1].lower()
        if side not in ("long", "short"):
            await update.message.reply_text("Side must be 'long' or 'short'.")
            return
        size = float(args[2])
        leverage = int(args[3])
        tp = float(args[4]) if len(args) > 4 else None
        sl = float(args[5]) if len(args) > 5 else None
        is_buy = side == "long"

        client = _get_client(update.effective_user.id)
        price = client.get_mid_price(coin)
        notional = price * size
        margin = notional / leverage
        extras = ""
        if tp:
            extras += f"\n   Take-profit: ${tp:,.2f}"
        if sl:
            extras += f"\n   Stop-loss:   ${sl:,.2f}"

        preview = (
            f"Order Preview\n\n"
            f"{'🟢' if is_buy else '🔴'} {coin} {'LONG' if is_buy else 'SHORT'}\n"
            f"   Size:      {size} {coin}\n"
            f"   Price:     ~${price:,.2f}\n"
            f"   Notional:  ~${notional:,.2f}\n"
            f"   Leverage:  {leverage}x\n"
            f"   Margin:    ~${margin:,.2f}"
            f"{extras}\n\n"
            f"Send /confirm to execute or /dismiss to cancel.\n"
            f"Expires in {CONFIRM_TTL}s."
        )
        _store_pending(
            update.effective_user.id, preview,
            lambda: client.open_position(
                coin=coin, is_buy=is_buy, size=size,
                leverage=leverage, tp=tp, sl=sl,
            ),
        )
        await update.message.reply_text(preview)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}\n\n/help open")


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /close <coin> [size]\n\nExample: /close BTC\n\n/help close"
        )
        return
    try:
        coin = args[0].upper()
        size = float(args[1]) if len(args) > 1 else None

        client = _get_client(update.effective_user.id)
        pos = client._find_position(coin)
        if not pos:
            await update.message.reply_text(
                f"No open position for {coin}.\n\nCheck /positions"
            )
            return

        pos_size = float(pos["szi"])
        max_size = abs(pos_size)
        if size is not None and size > max_size:
            await update.message.reply_text(
                f"Size {size} exceeds position size {max_size}.\n\nCheck /positions"
            )
            return
        close_size = size if size is not None else max_size
        is_long = pos_size > 0
        price = client.get_mid_price(coin)
        pnl = (price - float(pos["entryPx"])) * close_size * (1 if is_long else -1)
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        label = f"{close_size} (full)" if size is None else f"{close_size} (partial)"

        preview = (
            f"Order Preview\n\n"
            f"{'🟢' if is_long else '🔴'} Close {coin} {'LONG' if is_long else 'SHORT'}\n"
            f"   Size:    {label}\n"
            f"   Price:   ~${price:,.2f}\n"
            f"   Est PnL: {pnl_str} {'📈' if pnl >= 0 else '📉'}\n\n"
            f"Send /confirm to execute or /dismiss to cancel.\n"
            f"Expires in {CONFIRM_TTL}s."
        )
        _store_pending(
            update.effective_user.id, preview,
            lambda: client.close_position(coin=coin, size=size),
        )
        await update.message.reply_text(preview)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}\n\n/help close")


async def cmd_ladder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            "Tap below to open the ladder form, or use:\n"
            "/ladder <coin> <parts> <from> <to>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("Close Position Form", web_app=WebAppInfo(url=LADDER_FORM_URL))]],
                resize_keyboard=True, one_time_keyboard=True,
            ),
        )
        return
    try:
        coin       = args[0].upper()
        n_parts    = int(args[1])
        from_price = float(args[2])
        to_price   = float(args[3])

        if n_parts < 2 or n_parts > 20:
            await update.message.reply_text("Number of parts must be between 2 and 20.")
            return
        if from_price <= 0 or to_price <= 0:
            await update.message.reply_text("Prices must be greater than 0.")
            return
        if from_price == to_price:
            await update.message.reply_text("from_price and to_price must be different.")
            return

        client = _get_client(update.effective_user.id)
        pos = client._find_position(coin)
        if not pos:
            await update.message.reply_text(f"No open {coin} position.\n\nCheck /positions")
            return

        pos_size  = float(pos["szi"])
        is_long   = pos_size > 0
        total_sz  = abs(pos_size)
        per_order = round(total_sz / n_parts, 8)

        from math import floor, log10
        def _round_px(px):
            if px <= 0: return px
            mag = int(floor(log10(abs(px))))
            return round(px, 4 - mag)

        prices = [
            _round_px(from_price + (to_price - from_price) * i / (n_parts - 1))
            for i in range(n_parts)
        ]

        # Build preview lines
        order_lines = []
        placed = 0.0
        for i, px in enumerate(prices):
            sz = round(total_sz - placed, 8) if i == n_parts - 1 else per_order
            placed = round(placed + sz, 8)
            order_lines.append(f"   {i+1}. {sz} {coin} @ ${px:,.2f}")

        direction = "above → below" if from_price > to_price else "below → above"
        preview = (
            f"Ladder Close Preview\n\n"
            f"{'🟢' if is_long else '🔴'} {coin} {'LONG' if is_long else 'SHORT'}\n"
            f"   Total size: {total_sz} {coin}\n"
            f"   Orders:     {n_parts}  ({direction})\n"
            f"   Per order:  ~{per_order} {coin}\n\n"
            + "\n".join(order_lines) +
            f"\n\nAll orders are reduce-only limit (GTC).\n"
            f"Send /confirm to place all {n_parts} orders or /dismiss to cancel.\n"
            f"Expires in {CONFIRM_TTL}s."
        )
        _store_pending(
            update.effective_user.id, preview,
            lambda c=coin, n=n_parts, fp=from_price, tp=to_price: client.ladder_close(c, n, fp, tp),
        )
        await update.message.reply_text(preview)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}\n\n/help ladder")


async def cmd_slladder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            "Tap below to open the ladder form, or use:\n"
            "/slladder <coin> <parts> <from> <to>",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("Close Position Form", web_app=WebAppInfo(url=LADDER_FORM_URL))]],
                resize_keyboard=True, one_time_keyboard=True,
            ),
        )
        return
    try:
        coin       = args[0].upper()
        n_parts    = int(args[1])
        from_price = float(args[2])
        to_price   = float(args[3])

        if n_parts < 2 or n_parts > 20:
            await update.message.reply_text("Number of parts must be between 2 and 20.")
            return
        if from_price <= 0 or to_price <= 0:
            await update.message.reply_text("Prices must be greater than 0.")
            return
        if from_price == to_price:
            await update.message.reply_text("from_price and to_price must be different.")
            return

        client = _get_client(update.effective_user.id)
        pos = client._find_position(coin)
        if not pos:
            await update.message.reply_text(f"No open {coin} position.\n\nCheck /positions")
            return

        pos_size  = float(pos["szi"])
        is_long   = pos_size > 0
        total_sz  = abs(pos_size)
        per_order = round(total_sz / n_parts, 8)
        mid       = client.get_mid_price(coin)

        from math import floor, log10
        def _round_px(px):
            if px <= 0: return px
            mag = int(floor(log10(abs(px))))
            return round(px, 4 - mag)

        prices = [
            _round_px(from_price + (to_price - from_price) * i / (n_parts - 1))
            for i in range(n_parts)
        ]

        # Warn if prices are on the wrong side (would fire immediately)
        warning = ""
        if is_long and any(p >= mid for p in prices):
            warning = "\n⚠️ Some prices are above current market — those triggers will fire immediately."
        elif not is_long and any(p <= mid for p in prices):
            warning = "\n⚠️ Some prices are below current market — those triggers will fire immediately."

        order_lines = []
        placed = 0.0
        for i, px in enumerate(prices):
            sz = round(total_sz - placed, 8) if i == n_parts - 1 else per_order
            placed = round(placed + sz, 8)
            order_lines.append(f"   {i+1}. {sz} {coin} @ ${px:,.2f} (trigger)")

        preview = (
            f"SL Ladder Preview\n\n"
            f"{'🟢' if is_long else '🔴'} {coin} {'LONG' if is_long else 'SHORT'}\n"
            f"   Current:    ~${mid:,.2f}\n"
            f"   Total size: {total_sz} {coin}\n"
            f"   Orders:     {n_parts}\n"
            f"   Per order:  ~{per_order} {coin}\n\n"
            + "\n".join(order_lines) +
            f"{warning}\n\n"
            f"Each trigger closes that slice at market when price hits it.\n"
            f"Send /confirm to place all {n_parts} stop triggers or /dismiss to cancel.\n"
            f"Expires in {CONFIRM_TTL}s."
        )
        _store_pending(
            update.effective_user.id, preview,
            lambda c=coin, n=n_parts, fp=from_price, tp=to_price: client.slladder_close(c, n, fp, tp),
        )
        await update.message.reply_text(preview)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}\n\n/help slladder")


_CHART_INTERVALS  = {"5m", "15m", "1h", "4h", "1d"}
_CHART_HOURS      = {"5m": 24, "15m": 24, "1h": 24, "4h": 7*24, "1d": 30*24}
_CHART_TF_ORDER   = ["5m", "15m", "1h", "4h", "1d"]
_CHART_COINS      = ["BTC", "ETH", "SOL", "AVAX", "ARB", "DOGE"]


def _chart_interval_keyboard(coin: str, current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"{'▸ ' if tf == current else ''}{tf}",
            callback_data=f"chart_tf:{coin}:{tf}",
        )
        for tf in _CHART_TF_ORDER
    ]])


def _build_chart(candles: list, coin: str, interval: str) -> "io.BytesIO":
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import matplotlib.ticker as mticker
    from datetime import datetime, timezone

    if not candles:
        raise ValueError("No candle data returned.")

    times  = [datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc) for c in candles]
    opens  = [float(c["o"]) for c in candles]
    highs  = [float(c["h"]) for c in candles]
    lows   = [float(c["l"]) for c in candles]
    closes = [float(c["c"]) for c in candles]
    vols   = [float(c["v"]) for c in candles]

    BG, UP, DOWN, GRID, TEXT = "#18181b", "#16a34a", "#dc2626", "#27272a", "#a1a1aa"

    fig, (ax, vax) = plt.subplots(
        2, 1, figsize=(12, 7),
        gridspec_kw={"height_ratios": [4, 1], "hspace": 0.04},
        facecolor=BG,
    )
    ax.set_facecolor(BG)
    vax.set_facecolor(BG)

    n, W = len(candles), 0.6
    for i, (o, h, l, c) in enumerate(zip(opens, highs, lows, closes)):
        color = UP if c >= o else DOWN
        ax.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=2)
        rect = mpatches.FancyBboxPatch(
            (i - W / 2, min(o, c)), W, max(abs(c - o), (h - l) * 0.005),
            boxstyle="square,pad=0", linewidth=0, facecolor=color, zorder=3,
        )
        ax.add_patch(rect)

    for i, (o, c, v) in enumerate(zip(opens, closes, vols)):
        vax.bar(i, v, width=W, color=UP if c >= o else DOWN, alpha=0.6)

    step = max(1, n // 8)
    tick_pos = list(range(0, n, step))
    for a in (ax, vax):
        a.set_xlim(-0.5, n - 0.5)
        a.set_xticks(tick_pos)
        a.tick_params(colors=TEXT, labelsize=8)
        for spine in a.spines.values():
            spine.set_edgecolor(GRID)
        a.grid(axis="y", color=GRID, linewidth=0.5)

    ax.set_xticklabels([])
    vax.set_xticklabels(
        [times[i].strftime("%m/%d %H:%M") for i in tick_pos],
        rotation=30, ha="right", fontsize=7, color=TEXT,
    )

    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    vax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.2f}"))
    ax.tick_params(axis="y", colors=TEXT)
    vax.tick_params(axis="y", colors=TEXT)

    last = closes[-1]
    ax.axhline(last, color=TEXT, linewidth=0.6, linestyle="--", alpha=0.5)
    ax.text(n - 0.3, last, f"  ${last:,.2f}", color=TEXT, fontsize=8, va="center")

    pct   = (closes[-1] - opens[0]) / opens[0] * 100
    sign  = "+" if pct >= 0 else ""
    hours = _CHART_HOURS.get(interval, 24)
    period = f"{hours}h" if hours < 24 * 7 else f"{hours // 24}d"
    ax.set_title(
        f"{coin}  {interval}  |  {period}: {sign}{pct:.2f}%   Last: ${last:,.2f}",
        color=TEXT, fontsize=11, loc="left", pad=10,
    )
    ax.set_ylabel("Price (USDC)", color=TEXT, fontsize=8)
    vax.set_ylabel("Vol", color=TEXT, fontsize=8)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


async def _send_chart(bot, chat_id: int, tg_id: int, coin: str, interval: str):
    client  = _get_client(tg_id)
    hours   = _CHART_HOURS[interval]
    candles = client.get_candles(coin, interval, hours=hours)
    buf     = _build_chart(candles, coin, interval)
    await bot.send_photo(
        chat_id=chat_id,
        photo=buf,
        reply_markup=_chart_interval_keyboard(coin, interval),
    )


async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args

    if not args:
        await update.message.reply_text(
            "Pick a coin:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(c, callback_data=f"chart_tf:{c}:1h")
                for c in _CHART_COINS[:3]
            ], [
                InlineKeyboardButton(c, callback_data=f"chart_tf:{c}:1h")
                for c in _CHART_COINS[3:]
            ]]),
        )
        return

    coin     = args[0].upper()
    interval = args[1].lower() if len(args) > 1 else "1h"
    if interval not in _CHART_INTERVALS:
        await update.message.reply_text(
            f"Unknown interval '{interval}'.\n"
            f"Valid: {', '.join(_CHART_TF_ORDER)}"
        )
        return

    msg = await update.message.reply_text(f"Loading {coin} {interval}…")
    try:
        await _send_chart(
            context.bot, update.effective_chat.id,
            update.effective_user.id, coin, interval,
        )
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def handle_chart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_id = query.from_user.id
    if not _is_allowed(tg_id):
        return
    _, coin, interval = query.data.split(":")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
        await _send_chart(
            context.bot, query.message.chat_id,
            tg_id, coin, interval,
        )
    except Exception as e:
        await context.bot.send_message(chat_id=tg_id, text=f"Error: {e}")


async def cmd_overview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    await update.message.reply_text("Fetching overview…")
    try:
        client = _get_client(update.effective_user.id)
        prices = client.get_prices()
        ctx    = client.get_asset_contexts()

        charts = []
        for coin in ("BTC", "ETH"):
            candles = client.get_candles(coin, "1h", hours=24)
            buf     = _build_chart(candles, coin, "1h")

            px      = float(prices.get(coin, 0))
            hi      = max(float(c["h"]) for c in candles)
            lo      = min(float(c["l"]) for c in candles)
            open24  = float(candles[0]["o"]) if candles else px
            chg_pct = (px - open24) / open24 * 100 if open24 else 0
            chg_sign = "+" if chg_pct >= 0 else ""
            chg_emoji = "📈" if chg_pct >= 0 else "📉"

            funding_raw = ctx.get(coin, {}).get("funding")
            oi_raw      = ctx.get(coin, {}).get("openInterest")
            funding_str = f"{float(funding_raw)*100:+.4f}%/8h" if funding_raw is not None else "n/a"
            oi_str      = f"${float(oi_raw)/1e9:.2f}B" if oi_raw is not None else "n/a"

            caption = (
                f"{chg_emoji} <b>{coin}</b>  ${px:,.2f}\n"
                f"24h: <b>{chg_sign}{chg_pct:.2f}%</b>   "
                f"H ${hi:,.2f}  /  L ${lo:,.2f}\n"
                f"Funding: <b>{funding_str}</b>   OI: {oi_str}"
            )
            charts.append((buf, caption))

        for buf, caption in charts:
            await update.message.reply_photo(photo=buf, caption=caption, parse_mode="HTML")

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_tp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /tp <coin> <price> [size]\n\nExample: /tp BTC 75000\n\n/help tp"
        )
        return
    try:
        coin = args[0].upper()
        price = float(args[1])
        size = float(args[2]) if len(args) > 2 else None

        client = _get_client(update.effective_user.id)
        pos = client._find_position(coin)
        if not pos:
            await update.message.reply_text(f"No open position for {coin}.\n\nOpen one with /open")
            return

        is_long = float(pos["szi"]) > 0
        entry = float(pos["entryPx"])
        close_size = size or abs(float(pos["szi"]))
        pnl = (price - entry) * close_size * (1 if is_long else -1)
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        warning = ""
        if (is_long and price <= entry) or (not is_long and price >= entry):
            warning = "\n⚠️ Warning: TP price is not profitable vs your entry."

        preview = (
            f"Take-Profit Preview\n\n"
            f"{'🟢' if is_long else '🔴'} {coin} {'LONG' if is_long else 'SHORT'}\n"
            f"   Entry:      ${entry:,.2f}\n"
            f"   TP Trigger: ${price:,.2f}\n"
            f"   Size:       {close_size}\n"
            f"   Est PnL:    {pnl_str}"
            f"{warning}\n\n"
            f"Send /confirm to set or /dismiss to cancel.\n"
            f"Expires in {CONFIRM_TTL}s."
        )
        _store_pending(
            update.effective_user.id, preview,
            lambda: client.set_tp(coin=coin, trigger_price=price, size=size),
        )
        await update.message.reply_text(preview)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}\n\n/help tp")


async def cmd_sl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /sl <coin> <price> [size]\n\nExample: /sl BTC 55000\n\n/help sl"
        )
        return
    try:
        coin = args[0].upper()
        price = float(args[1])
        size = float(args[2]) if len(args) > 2 else None

        client = _get_client(update.effective_user.id)
        pos = client._find_position(coin)
        if not pos:
            await update.message.reply_text(f"No open position for {coin}.\n\nOpen one with /open")
            return

        is_long = float(pos["szi"]) > 0
        entry = float(pos["entryPx"])
        close_size = size or abs(float(pos["szi"]))
        loss = abs((price - entry) * close_size)
        warning = ""
        if (is_long and price >= entry) or (not is_long and price <= entry):
            warning = "\n⚠️ Warning: SL price is above your entry — this would lock in a profit, not a loss."

        preview = (
            f"Stop-Loss Preview\n\n"
            f"{'🟢' if is_long else '🔴'} {coin} {'LONG' if is_long else 'SHORT'}\n"
            f"   Entry:      ${entry:,.2f}\n"
            f"   SL Trigger: ${price:,.2f}\n"
            f"   Size:       {close_size}\n"
            f"   Max loss:   -${loss:.2f}"
            f"{warning}\n\n"
            f"Send /confirm to set or /dismiss to cancel.\n"
            f"Expires in {CONFIRM_TTL}s."
        )
        _store_pending(
            update.effective_user.id, preview,
            lambda: client.set_sl(coin=coin, trigger_price=price, size=size),
        )
        await update.message.reply_text(preview)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}\n\n/help sl")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /cancel <coin> <tp|sl|order_id>\n\n"
            "Examples:\n"
            "  /cancel BTC tp\n"
            "  /cancel BTC 54479543383\n\n"
            "/help cancel"
        )
        return
    try:
        coin, target = args[0].upper(), args[1]
        client = _get_client(update.effective_user.id)
        if target.lower() in ("tp", "sl"):
            results = client.cancel_tpsl(coin=coin, tpsl_type=target.lower())
            if not results:
                await update.message.reply_text(
                    f"No {target.upper()} orders found for {coin}.\n\n"
                    "Check active orders with /orders"
                )
            else:
                ok = sum(1 for r in results if r.get("status") == "ok")
                failed = len(results) - ok
                msg = f"Cancelled {ok} {target.upper()} order(s) for {coin}."
                if failed:
                    msg += f"\n{failed} failed — check /orders"
                await update.message.reply_text(msg)
        else:
            result = client.cancel_by_id(coin=coin, oid=int(target))
            await update.message.reply_text(_fmt_result(result))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}\n\n/help cancel")


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for /book — kept for backwards compatibility."""
    await cmd_book(update, context)


async def cmd_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unified account book: positions grouped with their orders + pending entries + balance."""
    if not await _guard(update):
        return
    try:
        client  = _get_client(update.effective_user.id)
        state   = client.get_positions()
        prices  = client.get_prices()
        spot    = client.get_spot_usdc()
        orders  = client.get_open_orders()
        summary = state.get("marginSummary", {})
        perp    = float(summary.get("accountValue", 0))

        open_positions = [
            e["position"] for e in state.get("assetPositions", [])
            if float(e["position"]["szi"]) != 0
        ]

        # Group orders by coin
        orders_by_coin: dict[str, list] = {}
        for o in orders:
            orders_by_coin.setdefault(o.get("coin", ""), []).append(o)

        position_coins = {p["coin"] for p in open_positions}

        # ── one card per position ─────────────────────────────────────────────
        if not open_positions and not orders:
            await update.message.reply_text(
                f"No open positions or orders.\n\n"
                f"💰 Balance\n"
                f"   Perp: ${perp:,.2f}\n"
                f"   Spot: ${spot:,.2f}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "⚡ Open Trade",
                        web_app=WebAppInfo(url=f"{WEB_APP_URL}&bal={perp:.2f}")
                    )
                ]]),
            )
            return

        for pos in open_positions:
            coin      = pos["coin"]
            coin_ords = orders_by_coin.get(coin, [])
            await update.message.reply_text(
                _position_text_with_orders(pos, prices, coin_ords),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("❌ Close 100%", callback_data=f"close_pct:{coin}:100"),
                        InlineKeyboardButton("✂️ 50%",        callback_data=f"close_pct:{coin}:50"),
                        InlineKeyboardButton("✂️ 25%",        callback_data=f"close_pct:{coin}:25"),
                    ],
                    [
                        InlineKeyboardButton("🎯 Set TP", callback_data=f"set_tp:{coin}"),
                        InlineKeyboardButton("🛑 Set SL", callback_data=f"set_sl:{coin}"),
                    ],
                    [
                        InlineKeyboardButton(
                            "🪜 Ladder Close",
                            web_app=WebAppInfo(url=f"{LADDER_FORM_URL}&coin={coin}")
                        ),
                    ],
                ]),
            )

        # ── pending entry orders for coins with no position ───────────────────
        entry_orders = [
            o for coin, ords in orders_by_coin.items()
            if coin not in position_coins
            for o in ords
        ]
        if entry_orders:
            lines = ["📋 Pending Entries\n"]
            for o in entry_orders:
                side = "BUY" if o.get("side") == "B" else "SELL"
                coin = o.get("coin", "?")
                px   = float(o.get("limitPx", 0))
                sz   = o.get("sz", "?")
                oid  = o.get("oid", "")
                lines.append(f"{'🟢' if side == 'BUY' else '🔴'} {coin} {side}  ${px:,.2f}  ({sz})  [oid:{oid}]")
            await update.message.reply_text("\n".join(lines))

        # ── balance footer with Open Trade button ─────────────────────────────
        await update.message.reply_text(
            f"💰 Balance\n"
            f"   Perp: ${perp:,.2f}\n"
            f"   Spot: ${spot:,.2f}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⚡ Open Trade",
                    web_app=WebAppInfo(url=f"{WEB_APP_URL}&bal={perp:.2f}")
                )
            ]]),
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /price <coin>\nExample: /price BTC")
        return
    try:
        coin = context.args[0].upper()
        price = _get_client(update.effective_user.id).get_mid_price(coin)
        await update.message.reply_text(f"{coin}: ${price:,.4f}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_assets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    try:
        assets = _get_client(update.effective_user.id).get_assets()
        lines = ["Perpetuals\n"]
        for a in assets:
            lines.append(f"{a['name']} — max {a.get('maxLeverage', '?')}x")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    try:
        client = _get_client(update.effective_user.id)
        fills = client.get_fills(days=7)
        if not fills:
            await update.message.reply_text("No trades in the last 7 days.")
            return

        total_pnl = 0.0
        total_fees = 0.0
        by_coin: dict[str, float] = {}
        for f in fills:
            pnl = float(f.get("closedPnl", 0))
            fee = float(f.get("fee", 0))
            coin = f.get("coin", "?")
            total_pnl += pnl
            total_fees += fee
            by_coin[coin] = by_coin.get(coin, 0) + pnl

        lines = ["7-Day Realised PnL\n"]
        for coin, pnl in sorted(by_coin.items(), key=lambda x: -abs(x[1])):
            sign = "+" if pnl >= 0 else ""
            lines.append(f"  {coin}: {sign}${pnl:.2f}")

        net = total_pnl - total_fees
        sign = "+" if total_pnl >= 0 else ""
        lines.append(f"\nGross PnL: {sign}${total_pnl:.2f}")
        lines.append(f"Fees paid: -${total_fees:.2f}")
        lines.append(f"Net PnL:   {'+'if net>=0 else ''}${net:.2f}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    try:
        client = _get_client(update.effective_user.id)
        fills  = client.get_fills(days=30)
        if not fills:
            await update.message.reply_text("No trades in the last 30 days.")
            return

        # Group closes by coin-side pair to identify trade P&L
        close_pnls: list[float] = []
        total_pnl = total_fees = 0.0
        daily_pnl: dict[str, float] = {}
        coin_pnl:  dict[str, float] = {}
        for f in fills:
            pnl  = float(f.get("closedPnl", 0))
            fee  = float(f.get("fee", 0))
            coin = f.get("coin", "?")
            ts   = float(f.get("time", 0)) / 1000
            day  = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            total_pnl  += pnl
            total_fees += fee
            if pnl != 0:
                close_pnls.append(pnl)
            daily_pnl[day] = daily_pnl.get(day, 0.0) + pnl
            coin_pnl[coin]  = coin_pnl.get(coin, 0.0) + pnl

        wins   = [p for p in close_pnls if p > 0]
        losses = [p for p in close_pnls if p < 0]
        n      = len(close_pnls)
        win_rt = len(wins) / n * 100 if n else 0
        avg_w  = sum(wins) / len(wins) if wins else 0
        avg_l  = sum(losses) / len(losses) if losses else 0
        net    = total_pnl - total_fees

        best_day  = max(daily_pnl.items(), key=lambda x: x[1]) if daily_pnl else None
        worst_day = min(daily_pnl.items(), key=lambda x: x[1]) if daily_pnl else None
        best_coin = max(coin_pnl.items(),  key=lambda x: x[1]) if coin_pnl else None
        worst_coin= min(coin_pnl.items(),  key=lambda x: x[1]) if coin_pnl else None

        sign   = "+" if total_pnl >= 0 else ""
        emoji  = "🟢" if net >= 0 else "🔴"
        lines = [f"📊 30-Day Trading Stats\n"]
        lines.append(f"{emoji} Net PnL:    {sign}${net:,.2f}  (after fees)")
        lines.append(f"   Gross:    {'+'if total_pnl>=0 else ''}${total_pnl:,.2f}")
        lines.append(f"   Fees:     -${total_fees:,.2f}")
        lines.append(f"\n📈 Win Rate:  {win_rt:.0f}%  ({len(wins)}W / {len(losses)}L / {n} total)")
        if avg_w: lines.append(f"   Avg win:  +${avg_w:,.2f}")
        if avg_l: lines.append(f"   Avg loss: -${abs(avg_l):,.2f}")
        if avg_w and avg_l:
            rr = avg_w / abs(avg_l)
            lines.append(f"   R:R ratio: {rr:.2f}")

        if best_day:
            bsign = "+" if best_day[1] >= 0 else ""
            lines.append(f"\n🏆 Best day:  {best_day[0]} ({bsign}${best_day[1]:,.2f})")
        if worst_day and worst_day[0] != (best_day[0] if best_day else ""):
            lines.append(f"💀 Worst day: {worst_day[0]} (-${abs(worst_day[1]):,.2f})")
        if best_coin:
            lines.append(f"\n🥇 Best coin:  {best_coin[0]}  +${best_coin[1]:,.2f}")
        if worst_coin and worst_coin[0] != (best_coin[0] if best_coin else ""):
            lines.append(f"💣 Worst coin: {worst_coin[0]}  -${abs(worst_coin[1]):,.2f}")

        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    try:
        client = _get_client(update.effective_user.id)
        state = client.get_positions()
        prices = client.get_prices()
        summary = state.get("marginSummary", {})
        account_val = float(summary.get("accountValue", 0))
        margin_used = float(summary.get("totalMarginUsed", 0))
        ntl = float(summary.get("totalNtlPos", 0))
        positions = [
            e for e in state.get("assetPositions", [])
            if float(e["position"]["szi"]) != 0
        ]

        if not positions:
            await update.message.reply_text("No open positions — no risk exposure.")
            return

        lines = ["Risk Overview\n"]
        lines.append(f"Total Notional: ${ntl:,.2f}")
        lines.append(f"Margin Used:    ${margin_used:,.2f}")
        lines.append(f"Account Value:  ${account_val:,.2f}")
        if ntl > 0:
            ratio = account_val / ntl * 100
            warn = " ⚠️" if ratio < 10 else ""
            lines.append(f"Margin Ratio:   {ratio:.1f}%{warn}")

        lines.append("")
        for e in positions:
            pos = e["position"]
            size = float(pos["szi"])
            mark = float(prices.get(pos["coin"], 0))
            liq = pos.get("liquidationPx")
            side = "LONG" if size > 0 else "SHORT"
            if liq and mark > 0:
                liq_f = float(liq)
                dist_pct = abs(mark - liq_f) / mark * 100
                warn = " ⚠️" if dist_pct < LIQ_WARN_PCT else ""
                lines.append(
                    f"{'🟢' if size>0 else '🔴'} {pos['coin']} {side}{warn}\n"
                    f"   Mark: ${mark:,.2f}  Liq: ${liq_f:,.2f}  ({dist_pct:.1f}% away)"
                )
            else:
                lines.append(f"{'🟢' if size>0 else '🔴'} {pos['coin']} {side} — cross margin")

        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /alert <coin> <price>\n\nExample: /alert BTC 70000\n\n/help alert"
        )
        return
    try:
        coin = args[0].upper()
        target = float(args[1])
        client = _get_client(update.effective_user.id)
        current = client.get_mid_price(coin)
        direction = "above" if target > current else "below"
        alert_id = db.add_alert(update.effective_user.id, coin, direction, target)
        await update.message.reply_text(
            f"Alert set!\n\n"
            f"{coin} is currently ${current:,.2f}\n"
            f"You'll be notified when it goes {direction} ${target:,.2f}\n\n"
            f"Alert ID: {alert_id} (use /cancelalert {alert_id} to remove)"
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    try:
        alerts = db.get_alerts(update.effective_user.id)
        if not alerts:
            await update.message.reply_text(
                "No active alerts.\n\nSet one with /alert <coin> <price>"
            )
            return
        lines = ["Active Alerts\n"]
        for a in alerts:
            arrow = "↑" if a["direction"] == "above" else "↓"
            lines.append(f"[{a['id']}] {a['coin']} {arrow} ${a['price']:,.2f}")
        lines.append("\nCancel with /cancelalert <id>")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_cancelalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /cancelalert <id>\n\nGet IDs from /alerts")
        return
    try:
        alert_id = int(context.args[0])
        alerts = db.get_alerts(update.effective_user.id)
        if not any(a["id"] == alert_id for a in alerts):
            await update.message.reply_text("Alert not found. Check /alerts")
            return
        db.delete_alert(alert_id)
        await update.message.reply_text(f"Alert {alert_id} cancelled.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


# ── admin commands ────────────────────────────────────────────────────────────

async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /adduser <telegram_id>")
        return
    try:
        new_id = int(context.args[0])
        db.add_allowed_user(new_id, added_by=update.effective_user.id)
        await update.message.reply_text(
            f"User {new_id} whitelisted.\n\n"
            f"They can now send /start to the bot and /register their HL account."
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeuser <telegram_id>")
        return
    try:
        rm_id = int(context.args[0])
        if _is_admin(rm_id):
            await update.message.reply_text("Cannot remove an admin user.")
            return
        db.remove_allowed_user(rm_id)
        _clients.pop((rm_id, os.getenv("NETWORK", "mainnet")), None)
        _snapshots.pop(rm_id, None)
        _pending.pop(rm_id, None)
        await update.message.reply_text(f"User {rm_id} removed. Their credentials have been deleted.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update):
        return
    try:
        allowed = db.get_allowed_users()
        lines = ["Whitelisted Users\n"]
        for u in allowed:
            reg = "registered" if db.is_registered(u["tg_id"]) else "not registered"
            lines.append(f"  {u['tg_id']} — {reg} (added {u['added_at'][:10]})")
        lines.append("\nAdmins (from .env):")
        for admin_id in ADMIN_IDS:
            reg = "registered" if db.is_registered(admin_id) else "not registered"
            lines.append(f"  {admin_id} — {reg} (admin)")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


# ── background notification polling ──────────────────────────────────────────

async def handle_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    import json
    try:
        data = json.loads(update.message.web_app_data.data)
        form_type      = data.get("form_type", "open")
        pre_confirmed  = bool(data.get("pre_confirmed", False))
        tg_id          = update.effective_user.id

        # ── ladder close (limit) ──────────────────────────────────────────────
        if form_type in ("ladder", "slladder"):
            coin       = data["coin"].upper()
            n_parts    = int(data["parts"])
            from_price = float(data["from_price"])
            to_price   = float(data["to_price"])
            size_raw   = data.get("size", "")
            size_close = float(size_raw) if size_raw and float(size_raw) > 0 else None  # reserved for future partial-close support

            client = _get_client(tg_id)

            type_label = "📊 Limit Ladder" if form_type == "ladder" else "🛑 SL Trigger Ladder"
            prices = [
                round(from_price + (to_price - from_price) * i / (n_parts - 1), 2)
                for i in range(n_parts)
            ]
            pct_each    = round(100 / n_parts, 1)
            levels_text = "\n".join(
                f"   {i+1}. {pct_each}% @ ${px:,.2f}" for i, px in enumerate(prices)
            )
            size_label = f"{size_close} {coin}" if size_close else "full position"
            preview = (
                f"{type_label}\n\n"
                f"{coin}  —  {size_label}  —  {n_parts} orders\n\n"
                f"{levels_text}"
            )
            fn = (
                (lambda c=coin, n=n_parts, fp=from_price, tp=to_price:
                 client.ladder_close(c, n, fp, tp))
                if form_type == "ladder"
                else
                (lambda c=coin, n=n_parts, fp=from_price, tp=to_price:
                 client.slladder_close(c, n, fp, tp))
            )
            if pre_confirmed:
                await update.message.reply_text(f"Executing…\n\n{preview}")
                result = fn()
                filled = sum(
                    1 for s in result.get("response", {}).get("data", {}).get("statuses", [])
                    if "resting" in s or "filled" in s
                )
                await update.message.reply_text(f"✅ {type_label} placed {filled}/{n_parts} orders for {coin}.")
            else:
                _store_pending(tg_id, preview + f"\n\nSend /confirm to execute or /dismiss to cancel.\nExpires in {CONFIRM_TTL}s.", fn)
                await update.message.reply_text(preview + f"\n\nSend /confirm to execute or /dismiss to cancel.\nExpires in {CONFIRM_TTL}s.")
            return

        # ── ladder open ───────────────────────────────────────────────────────
        if form_type == "open_ladder":
            coin       = data["coin"].upper()
            side       = data["side"]
            total_size = float(data["size"])
            leverage   = int(data["leverage"])
            n_parts    = int(data["parts"])
            from_price = float(data["from_price"])
            to_price   = float(data["to_price"])
            is_buy     = side == "long"

            client = _get_client(tg_id)
            prices_list = [
                round(from_price + (to_price - from_price) * i / (n_parts - 1), 2)
                for i in range(n_parts)
            ]
            pct_each    = round(100 / n_parts, 1)
            levels_text = "\n".join(
                f"   {i+1}. {pct_each}% @ ${px:,.2f}" for i, px in enumerate(prices_list)
            )
            dir_label = "🟢 LONG" if is_buy else "🔴 SHORT"
            preview = (
                f"Ladder Open\n\n"
                f"{dir_label} {coin}  {leverage}x  ({n_parts} orders)\n"
                f"Total size: {total_size} {coin}\n\n"
                f"{levels_text}"
            )
            fn = lambda c=coin, b=is_buy, sz=total_size, lev=leverage, n=n_parts, fp=from_price, tp=to_price: \
                client.ladder_open(c, b, sz, lev, n, fp, tp)
            if pre_confirmed:
                await update.message.reply_text(f"Executing…\n\n{preview}")
                result = fn()
                filled = sum(
                    1 for s in result.get("response", {}).get("data", {}).get("statuses", [])
                    if "resting" in s or "filled" in s
                )
                await update.message.reply_text(f"✅ Ladder placed {filled}/{n_parts} orders for {coin}.")
            else:
                _store_pending(tg_id, preview + f"\n\nSend /confirm to execute or /dismiss to cancel.\nExpires in {CONFIRM_TTL}s.", fn)
                await update.message.reply_text(preview + f"\n\nSend /confirm to execute or /dismiss to cancel.\nExpires in {CONFIRM_TTL}s.")
            return

        # ── single open order ─────────────────────────────────────────────────
        coin        = data["coin"].upper()
        side        = data["side"]
        size        = float(data["size"])
        leverage    = int(data["leverage"])
        tp          = float(data["tp"]) if data.get("tp") else None
        sl          = float(data["sl"]) if data.get("sl") else None
        limit_price = float(data["limit_price"]) if data.get("limit_price") else None
        is_buy      = side == "long"

        client        = _get_client(tg_id)
        mid           = client.get_mid_price(coin)
        display_price = limit_price if limit_price else mid
        notional      = display_price * size
        margin        = notional / leverage
        price_label   = f"${limit_price:,.2f} (limit)" if limit_price else f"~${mid:,.2f} (market)"
        extras = ""
        if tp:
            extras += f"\n   Take-profit: ${tp:,.2f}"
        if sl:
            extras += f"\n   Stop-loss:   ${sl:,.2f}"

        preview = (
            f"Order Preview\n\n"
            f"{'🟢' if is_buy else '🔴'} {coin} {'LONG' if is_buy else 'SHORT'}\n"
            f"   Size:      {size} {coin}\n"
            f"   Price:     {price_label}\n"
            f"   Notional:  ~${notional:,.2f}\n"
            f"   Leverage:  {leverage}x\n"
            f"   Margin:    ~${margin:,.2f}"
            f"{extras}"
        )
        fn = lambda: client.open_position(
            coin=coin, is_buy=is_buy, size=size,
            leverage=leverage, limit_px=limit_price, tp=tp, sl=sl,
        )
        if pre_confirmed:
            await update.message.reply_text(f"Executing…\n\n{preview}")
            fn()
            await update.message.reply_text(f"✅ {'Long' if is_buy else 'Short'} {size} {coin} order submitted.")
        else:
            _store_pending(tg_id, preview + f"\n\nSend /confirm to execute or /dismiss to cancel.\nExpires in {CONFIRM_TTL}s.", fn)
            await update.message.reply_text(preview + f"\n\nSend /confirm to execute or /dismiss to cancel.\nExpires in {CONFIRM_TTL}s.")
    except Exception as e:
        await update.message.reply_text(f"Error processing form: {e}")


def _load_price_history():
    import json as _json
    try:
        with open(PRICE_HISTORY_FILE) as f:
            raw = _json.load(f)
        cutoff = time.time() - 25 * 3600
        for coin, entries in raw.items():
            _price_history[coin] = [(t, p) for t, p in entries if t >= cutoff]
        logging.info(f"Loaded price history for {len(_price_history)} coins.")
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.warning(f"Could not load price history: {e}")


def _save_price_history():
    import json as _json
    try:
        with open(PRICE_HISTORY_FILE, "w") as f:
            _json.dump(_price_history, f)
    except Exception as e:
        logging.warning(f"Could not save price history: {e}")


async def _persist_price_history(context: ContextTypes.DEFAULT_TYPE):
    _save_price_history()


def _build_digest(fills: list, positions: list) -> Optional[str]:
    """Build digest text from today's fills and current positions. Returns None if nothing to report."""
    now_ms = time.time() * 1000
    day_ms = 24 * 3600 * 1000
    day_fills = [f for f in fills if now_ms - float(f.get("time", 0)) < day_ms]

    coin_pnl: dict[str, float] = {}
    coin_trades: dict[str, int] = {}
    for fill in day_fills:
        coin = fill.get("coin", "?")
        coin_pnl[coin] = coin_pnl.get(coin, 0.0) + float(fill.get("closedPnl", 0))
        coin_trades[coin] = coin_trades.get(coin, 0) + 1

    total_pnl    = sum(coin_pnl.values())
    total_trades = sum(coin_trades.values())
    unrealized   = sum(float(p.get("unrealizedPnl", 0)) for p in positions)
    open_count   = len(positions)

    if total_trades == 0 and open_count == 0:
        return None

    emoji = "🟢" if total_pnl >= 0 else "🔴"
    sign  = "+" if total_pnl >= 0 else ""
    lines = [f"📊 Daily PnL Digest\n"]

    if total_trades > 0:
        lines.append(f"{emoji} Realized today: {sign}${total_pnl:,.2f}")
        lines.append(f"📝 Trades closed: {total_trades}")

        if coin_pnl:
            best  = max(coin_pnl, key=coin_pnl.get)
            worst = min(coin_pnl, key=coin_pnl.get)
            if coin_pnl[best] > 0:
                lines.append(f"🏆 Best:  {best} +${coin_pnl[best]:,.2f}")
            if coin_pnl[worst] < 0:
                lines.append(f"💀 Worst: {worst} −${abs(coin_pnl[worst]):,.2f}")
    else:
        lines.append("No closed trades today.")

    if open_count > 0:
        u_sign = "+" if unrealized >= 0 else ""
        u_emoji = "📈" if unrealized >= 0 else "📉"
        lines.append(f"\n{u_emoji} Unrealized: {u_sign}${unrealized:,.2f} ({open_count} open position{'s' if open_count != 1 else ''})")

    return "\n".join(lines)


async def _send_digest_to_user(bot, tg_id: int):
    try:
        client = _get_client(tg_id)
        fills  = client.get_fills(days=1)
        state  = client.get_positions()
        positions = [
            e["position"] for e in state.get("assetPositions", [])
            if float(e["position"]["szi"]) != 0
        ]
        text = _build_digest(fills, positions)
        if text:
            await bot.send_message(chat_id=tg_id, text=text)
    except Exception as e:
        logging.warning(f"Daily digest failed for {tg_id}: {e}")


async def _daily_digest(context: ContextTypes.DEFAULT_TYPE):
    import asyncio
    users = db.get_all_registered_users()
    for user in users:
        tg_id = user["tg_id"]
        if _is_allowed(tg_id):
            await _send_digest_to_user(context.bot, tg_id)
            await asyncio.sleep(0.05)  # 20 msg/s — safely under Telegram's 30/s limit


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    await update.message.reply_text("Fetching your digest…")
    await _send_digest_to_user(context.bot, update.effective_user.id)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    users = db.get_all_registered_users()
    all_ids = {u["tg_id"] for u in users} | ADMIN_IDS
    import asyncio
    sent = failed = 0
    for tg_id in all_ids:
        try:
            await context.bot.send_message(chat_id=tg_id, text=text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # 20 msg/s — safely under Telegram's 30/s limit
    await update.message.reply_text(f"Broadcast done: {sent} sent, {failed} failed.")


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        f"Unknown command: {update.message.text.split()[0]}\n\n"
        "Send /help to see all available commands."
    )


async def _poll_notifications(context: ContextTypes.DEFAULT_TYPE):
    users = db.get_all_registered_users()
    all_alerts = db.get_all_alerts()

    for user in users:
        tg_id = user["tg_id"]
        if not _is_allowed(tg_id):
            continue
        try:
            client = _get_client(tg_id)
            prices = client.get_prices()

            # Price alerts
            user_alerts = [a for a in all_alerts if a["tg_id"] == tg_id]
            for alert in user_alerts:
                current = float(prices.get(alert["coin"], 0))
                if current == 0:
                    continue
                triggered = (
                    (alert["direction"] == "above" and current >= alert["price"]) or
                    (alert["direction"] == "below" and current <= alert["price"])
                )
                if triggered:
                    db.delete_alert(alert["id"])
                    arrow = "↑" if alert["direction"] == "above" else "↓"
                    await context.bot.send_message(
                        chat_id=tg_id,
                        text=(
                            f"🔔 Price Alert\n\n"
                            f"{alert['coin']} hit ${current:,.2f}\n"
                            f"Your target: {arrow} ${alert['price']:,.2f}"
                        ),
                    )

            # Positions: fill detection + liquidation warning
            state = client.get_positions()
            positions = [
                e for e in state.get("assetPositions", [])
                if float(e["position"]["szi"]) != 0
            ]
            current_positions = {
                e["position"]["coin"]: float(e["position"]["szi"])
                for e in positions
            }

            for e in positions:
                pos = e["position"]
                liq = pos.get("liquidationPx")
                if not liq:
                    continue
                coin = pos["coin"]
                mark = float(prices.get(coin, 0))
                if mark == 0:
                    continue
                liq_f = float(liq)
                dist_pct = abs(mark - liq_f) / mark * 100
                warn_key = f"{tg_id}_{coin}"
                if dist_pct < LIQ_WARN_PCT:
                    if not _liq_warned.get(warn_key):
                        _liq_warned[warn_key] = True
                        side = "LONG" if float(pos["szi"]) > 0 else "SHORT"
                        await context.bot.send_message(
                            chat_id=tg_id,
                            text=(
                                f"⚠️ LIQUIDATION WARNING\n\n"
                                f"{coin} {side} is {dist_pct:.1f}% from liquidation!\n"
                                f"Mark: ${mark:,.2f}\n"
                                f"Liq:  ${liq_f:,.2f}\n\n"
                                f"Consider /close {coin} or adding margin."
                            ),
                        )
                else:
                    _liq_warned.pop(warn_key, None)

            # Clear liq_warned keys for positions that no longer exist
            stale = [k for k in list(_liq_warned) if k.startswith(f"{tg_id}_") and k[len(f"{tg_id}_"):] not in current_positions]
            for k in stale:
                _liq_warned.pop(k, None)

            # Auto move alerts — BTC + ETH always, plus any coin with an open position
            now_ts = time.time()
            _price_history_cutoff = now_ts - 25 * 3600  # keep 25h
            watch_coins = {"BTC", "ETH"} | set(current_positions.keys())
            for coin in watch_coins:
                px = float(prices.get(coin, 0))
                if px == 0:
                    continue
                hist = _price_history.setdefault(coin, [])
                hist.append((now_ts, px))
                # prune old entries
                _price_history[coin] = [(t, p) for t, p in hist if t >= _price_history_cutoff]

                for tier, window_secs, threshold in [
                    ("1h",  3600,      MOVE_1H_PCT),
                    ("24h", 86400,     MOVE_24H_PCT),
                ]:
                    cutoff = now_ts - window_secs
                    old_entries = [(t, p) for t, p in _price_history[coin] if t <= cutoff]
                    if not old_entries:
                        continue
                    ref_px = old_entries[-1][1]  # closest entry at/before the window edge
                    move_pct = (px - ref_px) / ref_px * 100
                    if abs(move_pct) < threshold:
                        continue
                    cooldown_key = f"{tg_id}_{coin}_{tier}"
                    last_alerted = _move_alerted.get(cooldown_key, 0)
                    if now_ts - last_alerted < MOVE_COOLDOWN:
                        continue
                    _move_alerted[cooldown_key] = now_ts
                    direction = "📈" if move_pct > 0 else "📉"
                    sign = "+" if move_pct > 0 else ""
                    await context.bot.send_message(
                        chat_id=tg_id,
                        text=(
                            f"{direction} Move Alert — {coin}\n\n"
                            f"{sign}{move_pct:.1f}% in the last {tier}\n"
                            f"Current: ${px:,.2f}   Ref: ${ref_px:,.2f}"
                        ),
                    )

            # Fill notifications via get_fills() with timestamp tracking.
            # _last_fill_ts uses None as sentinel for "first poll since boot"
            # so we never replay history on restart.
            try:
                fills = client.get_fills(days=1)
                last_ts = _last_fill_ts.get(tg_id)  # None = never seeded
                if last_ts is None:
                    # First poll after (re)start — seed cursor, send nothing
                    _last_fill_ts[tg_id] = max(
                        (float(f.get("time", 0)) for f in fills),
                        default=time.time() * 1000,
                    )
                else:
                    new_last = last_ts
                    for f in sorted(fills, key=lambda x: float(x.get("time", 0))):
                        fill_ts = float(f.get("time", 0))
                        if fill_ts <= last_ts:
                            continue
                        new_last = max(new_last, fill_ts)
                        coin        = f.get("coin", "?")
                        fill_px     = float(f.get("px", 0))
                        fill_sz     = float(f.get("sz", 0))
                        is_buy      = f.get("side") == "B"
                        closed_pnl  = float(f.get("closedPnl", 0))
                        order_type  = f.get("orderType", "")

                        if "Take Profit" in order_type:
                            pnl_str = f"+${closed_pnl:,.2f}" if closed_pnl >= 0 else f"-${abs(closed_pnl):,.2f}"
                            msg = f"🎯 Take-Profit filled!\n\n{coin} @ ${fill_px:,.2f}\nPnL: {pnl_str}"
                        elif "Stop" in order_type:
                            pnl_str = f"+${closed_pnl:,.2f}" if closed_pnl >= 0 else f"-${abs(closed_pnl):,.2f}"
                            msg = f"🛑 Stop-Loss filled!\n\n{coin} @ ${fill_px:,.2f}\nPnL: {pnl_str}"
                        elif closed_pnl != 0:
                            pnl_str = f"+${closed_pnl:,.2f}" if closed_pnl >= 0 else f"-${abs(closed_pnl):,.2f}"
                            side_lbl = "BUY" if is_buy else "SELL"
                            msg = f"✅ Fill: {coin} {side_lbl} {fill_sz} @ ${fill_px:,.2f}\nPnL: {pnl_str}"
                        else:
                            side_lbl = "BUY" if is_buy else "SELL"
                            msg = f"✅ Fill: {coin} {side_lbl} {fill_sz} @ ${fill_px:,.2f}"

                        await context.bot.send_message(chat_id=tg_id, text=msg)

                    if new_last > last_ts:
                        _last_fill_ts[tg_id] = new_last
            except Exception:
                pass  # fills are best-effort, don't break the poll loop

            # Reset error counter on success
            _poll_errors.pop(tg_id, None)
            _poll_error_alerted.discard(tg_id)

        except Exception as e:
            logging.debug(f"Notification poll failed for {tg_id}: {e}")
            count = _poll_errors.get(tg_id, 0) + 1
            _poll_errors[tg_id] = count
            if count >= 3 and tg_id not in _poll_error_alerted:
                _poll_error_alerted.add(tg_id)
                for admin_id in ADMIN_IDS:
                    try:
                        await context.bot.send_message(
                            chat_id=admin_id,
                            text=(
                                f"⚠️ Bot health alert\n\n"
                                f"Poll failing for user {tg_id} "
                                f"({count} consecutive errors).\n"
                                f"Last error: {e}\n\n"
                                f"Check: journalctl -u hl-bot -n 50"
                            ),
                        )
                    except Exception:
                        pass


# ── entry point ───────────────────────────────────────────────────────────────

async def _post_init(app: Application):
    await app.bot.set_my_commands([
        # ── top-level actions ────────────────────────────
        BotCommand("overview",    "BTC & ETH charts, prices, funding rates"),
        BotCommand("book",        "Positions + orders + balance (full book)"),
        BotCommand("positions",   "Alias for /book"),
        BotCommand("open",        "Open a position"),
        BotCommand("close",       "Close a position"),
        BotCommand("ladder",      "Scaled close — /ladder ETH 5 3500 3000"),
        # ── order management ─────────────────────────────
        BotCommand("slladder",    "Scaled stop — /slladder ETH 5 2900 2600"),
        BotCommand("tp",          "Set take-profit"),
        BotCommand("sl",          "Set stop-loss"),
        BotCommand("cancel",      "Cancel an order"),
        BotCommand("orders",      "Resting orders"),
        BotCommand("confirm",     "Execute previewed order"),
        BotCommand("dismiss",     "Discard previewed order"),
        # ── account & analytics ──────────────────────────
        BotCommand("pnl",         "7-day realised PnL"),
        BotCommand("stats",       "30-day win rate, R:R, best/worst"),
        BotCommand("digest",      "Today's PnL digest on demand"),
        BotCommand("risk",        "Margin & liquidation risk"),
        # ── market data ──────────────────────────────────
        BotCommand("chart",       "Candlestick chart — /chart BTC 1h"),
        BotCommand("price",       "Current price — /price BTC"),
        BotCommand("alert",       "Set price alert — /alert BTC 70000"),
        BotCommand("alerts",      "List active alerts"),
        BotCommand("assets",      "List tradeable markets"),
        # ── setup ────────────────────────────────────────
        BotCommand("register",    "Link your Hyperliquid account"),
        BotCommand("help",        "Command help"),
    ])


def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN not set in .env")
    db.init_db()
    _load_price_history()

    app = Application.builder().token(TOKEN).post_init(_post_init).build()

    for cmd, handler in [
        ("start",       cmd_start),
        ("help",        cmd_help),
        ("register",    cmd_register),
        ("confirm",     cmd_confirm),
        ("dismiss",     cmd_dismiss),
        ("book",        cmd_book),
        ("positions",   cmd_positions),
        ("open",        cmd_open),
        ("close",       cmd_close),
        ("ladder",      cmd_ladder),
        ("slladder",    cmd_slladder),
        ("tp",          cmd_tp),
        ("sl",          cmd_sl),
        ("cancel",      cmd_cancel),
        ("orders",      cmd_orders),
        ("overview",    cmd_overview),
        ("chart",       cmd_chart),
        ("price",       cmd_price),
        ("assets",      cmd_assets),
        ("pnl",         cmd_pnl),
        ("stats",       cmd_stats),
        ("risk",        cmd_risk),
        ("alert",       cmd_alert),
        ("alerts",      cmd_alerts),
        ("cancelalert", cmd_cancelalert),
        ("adduser",     cmd_adduser),
        ("removeuser",  cmd_removeuser),
        ("listusers",   cmd_listusers),
        ("digest",      cmd_digest),
        ("broadcast",   cmd_broadcast),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    app.job_queue.run_repeating(_poll_notifications, interval=POLL_INTERVAL, first=15)
    app.job_queue.run_repeating(_persist_price_history, interval=300, first=300)  # every 5 min
    app.job_queue.run_daily(_daily_digest, time=DIGEST_TIME)
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))
    app.add_handler(CallbackQueryHandler(handle_close_callback, pattern="^(close_pct:|close_partial:|set_tp:|set_sl:)"))
    app.add_handler(CallbackQueryHandler(handle_chart_callback, pattern="^chart_tf:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    # Must be last — catches any /command not matched above
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))

    logging.info("Bot started. Polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
