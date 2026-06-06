import logging
import os
import time
from typing import Optional

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import db
from client import HLClient

load_dotenv()

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)

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

CONFIRM_TTL = 60
POLL_INTERVAL = 30
LIQ_WARN_PCT = 15
WEB_APP_URL = "https://whynotvlad.github.io/hl-trade/open.html?v=6"

QUICK_KEYS = ReplyKeyboardMarkup(
    [["/positions", "/orders"], ["/pnl", "/price BTC"]],
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
            f"   Price: ${float(o.get('limitPx', 0)):,.2f}\n"
            f"   ID:    {o.get('oid')}"
        )
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
            "/positions — open positions\n"
            "/orders — resting orders\n"
            "/price BTC — current price\n"
            "/pnl — 7-day realised PnL\n"
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
            "/positions — open positions & balance",
            "/orders — resting orders",
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
        # Show updated positions and orders after every confirmed trade
        client = _get_client(update.effective_user.id)
        state  = client.get_positions()
        prices = client.get_prices()
        spot   = client.get_spot_usdc()
        await update.message.reply_text(_fmt_positions(state, prices, spot))
        orders = client.get_open_orders()
        if orders:
            await update.message.reply_text(_fmt_orders(orders))
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
    if not await _guard(update):
        return
    try:
        client = _get_client(update.effective_user.id)
        state = client.get_positions()
        prices = client.get_prices()
        spot = client.get_spot_usdc()

        open_positions = [
            e for e in state.get("assetPositions", [])
            if float(e["position"]["szi"]) != 0
        ]

        if not open_positions:
            summary = state.get("marginSummary", {})
            perp = float(summary.get("accountValue", 0))
            await update.message.reply_text(
                f"No open positions.\n\n"
                f"💰 Balance\n"
                f"   Perp: ${perp:,.2f}\n"
                f"   Spot: ${spot:,.2f}"
            )
            return

        for e in open_positions:
            pos = e["position"]
            coin = pos["coin"]
            await update.message.reply_text(
                _position_text(pos, prices),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("❌ Close Full",    callback_data=f"close_full:{coin}"),
                        InlineKeyboardButton("✂️ Close Partial", callback_data=f"close_partial:{coin}"),
                    ],
                    [
                        InlineKeyboardButton("🎯 Set TP", callback_data=f"set_tp:{coin}"),
                        InlineKeyboardButton("🛑 Set SL", callback_data=f"set_sl:{coin}"),
                    ],
                ]),
            )

        summary = state.get("marginSummary", {})
        perp = float(summary.get("accountValue", 0))
        await update.message.reply_text(
            f"💰 Balance\n"
            f"   Perp: ${perp:,.2f}\n"
            f"   Spot: ${spot:,.2f}"
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def handle_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_id = query.from_user.id
    if not _is_allowed(tg_id):
        return
    action, coin = query.data.split(":", 1)
    try:
        client = _get_client(tg_id)
        pos = client._find_position(coin)
        if not pos:
            await query.edit_message_text(f"No open {coin} position.")
            return

        pos_size = float(pos["szi"])
        is_long = pos_size > 0
        price = client.get_mid_price(coin)

        if action == "close_full":
            close_size = abs(pos_size)
            pnl = (price - float(pos["entryPx"])) * close_size * (1 if is_long else -1)
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            preview = (
                f"Order Preview\n\n"
                f"{'🟢' if is_long else '🔴'} Close {coin} {'LONG' if is_long else 'SHORT'}\n"
                f"   Size:    {close_size} (full)\n"
                f"   Price:   ~${price:,.2f}\n"
                f"   Est PnL: {pnl_str} {'📈' if pnl >= 0 else '📉'}\n\n"
                f"Send /confirm to execute or /dismiss to cancel.\n"
                f"Expires in {CONFIRM_TTL}s."
            )
            _store_pending(
                tg_id, preview,
                lambda c=coin: client.close_position(coin=c, size=None),
            )
            await context.bot.send_message(chat_id=tg_id, text=preview)

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
            "Usage: /ladder <coin> <parts> <from_price> <to_price>\n\n"
            "Example: /ladder ETH 5 3500 3000\n\n/help ladder"
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
            "Usage: /slladder <coin> <parts> <from_price> <to_price>\n\n"
            "Example: /slladder BTC 3 58000 55000\n\n/help slladder"
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


_CHART_INTERVALS = {"5m", "15m", "1h", "4h", "1d"}
_CHART_HOURS = {"5m": 24, "15m": 24, "1h": 24, "4h": 7*24, "1d": 30*24}


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


async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /chart <coin> [interval]\n\n"
            "Intervals: 5m  15m  1h  4h  1d\n"
            "Default:   1h\n\n"
            "Examples:\n"
            "  /chart BTC\n"
            "  /chart ETH 5m\n"
            "  /chart SOL 4h"
        )
        return
    coin     = args[0].upper()
    interval = args[1].lower() if len(args) > 1 else "1h"
    if interval not in _CHART_INTERVALS:
        await update.message.reply_text(
            f"Unknown interval '{interval}'.\n"
            f"Valid: {', '.join(sorted(_CHART_INTERVALS))}"
        )
        return
    msg = await update.message.reply_text(f"Fetching {coin} {interval} chart…")
    try:
        client = _get_client(update.effective_user.id)
        hours   = _CHART_HOURS[interval]
        candles = client.get_candles(coin, interval, hours=hours)
        buf     = _build_chart(candles, coin, interval)
        await update.message.reply_photo(photo=buf)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


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
    if not await _guard(update):
        return
    try:
        orders = _get_client(update.effective_user.id).get_open_orders()
        await update.message.reply_text(_fmt_orders(orders))
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
        coin        = data["coin"].upper()
        side        = data["side"]
        size        = float(data["size"])
        leverage    = int(data["leverage"])
        tp          = float(data["tp"]) if data.get("tp") else None
        sl          = float(data["sl"]) if data.get("sl") else None
        limit_price = float(data["limit_price"]) if data.get("limit_price") else None
        is_buy      = side == "long"

        client   = _get_client(update.effective_user.id)
        mid      = client.get_mid_price(coin)
        display_price = limit_price if limit_price else mid
        notional = display_price * size
        margin   = notional / leverage
        price_label = f"${limit_price:,.2f} (limit)" if limit_price else f"~${mid:,.2f} (market)"
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
            f"{extras}\n\n"
            f"Send /confirm to execute or /dismiss to cancel.\n"
            f"Expires in {CONFIRM_TTL}s."
        )
        _store_pending(
            update.effective_user.id, preview,
            lambda: client.open_position(
                coin=coin, is_buy=is_buy, size=size,
                leverage=leverage, limit_px=limit_price, tp=tp, sl=sl,
            ),
        )
        await update.message.reply_text(preview)
    except Exception as e:
        await update.message.reply_text(f"Error processing form: {e}")


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

            # Order fill detection
            orders = client.get_open_orders()
            current_oids = {o["oid"] for o in orders}

            if tg_id in _snapshots:
                snap = _snapshots[tg_id]
                disappeared = snap["order_ids"] - current_oids
                for oid in disappeared:
                    order = snap["orders"].get(oid, {})
                    coin = order.get("coin", "")
                    old_sz = snap["positions"].get(coin, 0)
                    new_sz = current_positions.get(coin, 0)
                    if abs(old_sz - new_sz) < 0.0001:
                        continue  # position didn't change — likely manual cancel
                    order_type = order.get("orderType", "Order")
                    px = float(order.get("limitPx", 0))
                    if "Take Profit" in order_type:
                        msg = f"✅ Take-Profit filled!\n\n{coin} closed at ${px:,.2f}"
                    elif "Stop" in order_type:
                        msg = f"🛑 Stop-Loss filled!\n\n{coin} closed at ${px:,.2f}"
                    else:
                        side = "BUY" if order.get("side") == "B" else "SELL"
                        msg = f"✅ Order filled!\n\n{coin} {side} at ${px:,.2f}"
                    await context.bot.send_message(chat_id=tg_id, text=msg)

            _snapshots[tg_id] = {
                "order_ids": current_oids,
                "orders": {o["oid"]: o for o in orders},
                "positions": current_positions,
            }

        except Exception as e:
            logging.debug(f"Notification poll failed for {tg_id}: {e}")


# ── entry point ───────────────────────────────────────────────────────────────

async def _post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("positions",   "Open positions & balance"),
        BotCommand("orders",      "Resting orders"),
        BotCommand("pnl",         "7-day realised PnL"),
        BotCommand("risk",        "Margin & liquidation risk"),
        BotCommand("chart",       "Candlestick chart — /chart BTC 1h"),
        BotCommand("price",       "Current price — /price BTC"),
        BotCommand("alert",       "Set price alert — /alert BTC 70000"),
        BotCommand("alerts",      "List active alerts"),
        BotCommand("open",        "Open a position"),
        BotCommand("close",       "Close a position"),
        BotCommand("ladder",      "Scaled exit — /ladder ETH 5 3500 3000"),
        BotCommand("slladder",    "Scaled stop — /slladder ETH 5 2900 2600"),
        BotCommand("tp",          "Set take-profit"),
        BotCommand("sl",          "Set stop-loss"),
        BotCommand("cancel",      "Cancel an order"),
        BotCommand("confirm",     "Execute previewed order"),
        BotCommand("dismiss",     "Discard previewed order"),
        BotCommand("assets",      "List tradeable markets"),
        BotCommand("help",        "Command help"),
        BotCommand("register",    "Link your Hyperliquid account"),
    ])


def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN not set in .env")
    db.init_db()

    app = Application.builder().token(TOKEN).post_init(_post_init).build()

    for cmd, handler in [
        ("start",       cmd_start),
        ("help",        cmd_help),
        ("register",    cmd_register),
        ("confirm",     cmd_confirm),
        ("dismiss",     cmd_dismiss),
        ("positions",   cmd_positions),
        ("open",        cmd_open),
        ("close",       cmd_close),
        ("ladder",      cmd_ladder),
        ("slladder",    cmd_slladder),
        ("tp",          cmd_tp),
        ("sl",          cmd_sl),
        ("cancel",      cmd_cancel),
        ("orders",      cmd_orders),
        ("chart",       cmd_chart),
        ("price",       cmd_price),
        ("assets",      cmd_assets),
        ("pnl",         cmd_pnl),
        ("risk",        cmd_risk),
        ("alert",       cmd_alert),
        ("alerts",      cmd_alerts),
        ("cancelalert", cmd_cancelalert),
        ("adduser",     cmd_adduser),
        ("removeuser",  cmd_removeuser),
        ("listusers",   cmd_listusers),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    app.job_queue.run_repeating(_poll_notifications, interval=POLL_INTERVAL, first=15)
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))
    app.add_handler(CallbackQueryHandler(handle_close_callback, pattern="^(close_|set_tp:|set_sl:)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    # Must be last — catches any /command not matched above
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))

    logging.info("Bot started. Polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
