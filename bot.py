import logging
import os
import time
from typing import Optional

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import db
from client import HLClient

load_dotenv()

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_IDS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_TG_IDS", "").split(",")
    if x.strip()
}

_clients: dict[tuple, HLClient] = {}
_pending: dict[int, dict] = {}  # tg_id -> {fn, preview, expires}

CONFIRM_TTL = 60  # seconds before a pending action expires

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
        "⚠️ Delete the message after sending — it contains your key."
    ),
    "open": (
        "Open a long or short position.\n\n"
        "Usage:\n"
        "  /open <coin> <long|short> <size> <leverage> [tp] [sl]\n\n"
        "Examples:\n"
        "  /open BTC long 0.001 10\n"
        "  /open ETH short 0.1 5\n"
        "  /open BTC long 0.001 10 75000 55000\n\n"
        "coin     — symbol from /assets (BTC, ETH, SOL…)\n"
        "size     — amount in base units (0.001 BTC ≈ $62)\n"
        "leverage — 1–50 depending on coin (check /assets)\n"
        "tp       — optional take-profit price\n"
        "sl       — optional stop-loss price\n\n"
        "Market orders execute immediately. "
        "Limit orders rest on the book until filled.\n\n"
        "You will be asked to confirm before the order is sent."
    ),
    "close": (
        "Close an open position fully or partially.\n\n"
        "Usage:\n"
        "  /close <coin> [size]\n\n"
        "Examples:\n"
        "  /close BTC          — close the full position\n"
        "  /close BTC 0.0005   — close half of a 0.001 BTC position\n\n"
        "You will be asked to confirm before the order is sent."
    ),
    "tp": (
        "Set a take-profit trigger on an open position.\n\n"
        "Usage:\n"
        "  /tp <coin> <price> [size]\n\n"
        "Examples:\n"
        "  /tp BTC 75000         — TP on full position\n"
        "  /tp BTC 75000 0.0005  — TP on partial size\n\n"
        "For a LONG, set price above your entry.\n"
        "For a SHORT, set price below your entry.\n\n"
        "The order executes at market when the trigger price is reached."
    ),
    "sl": (
        "Set a stop-loss trigger on an open position.\n\n"
        "Usage:\n"
        "  /sl <coin> <price> [size]\n\n"
        "Examples:\n"
        "  /sl BTC 55000         — SL on full position\n"
        "  /sl BTC 55000 0.0005  — SL on partial size\n\n"
        "For a LONG, set price below your entry.\n"
        "For a SHORT, set price above your entry.\n\n"
        "⚠️ If you partially close manually, update your SL size with /cancel then /sl."
    ),
    "cancel": (
        "Cancel a TP, SL, or any order by ID.\n\n"
        "Usage:\n"
        "  /cancel <coin> <tp|sl|order_id>\n\n"
        "Examples:\n"
        "  /cancel BTC tp          — cancel all BTC take-profit orders\n"
        "  /cancel BTC sl          — cancel all BTC stop-loss orders\n"
        "  /cancel BTC 54479543383 — cancel a specific order by ID\n\n"
        "Get order IDs from /orders."
    ),
    "positions": (
        "Show all open positions and account balance.\n\n"
        "Usage:\n"
        "  /positions\n\n"
        "Displays: coin, side, size, entry price, current mark price,\n"
        "unrealised PnL, leverage, and liquidation price (isolated only).\n\n"
        "Also shows perp account balance, spot USDC, and total."
    ),
    "orders": (
        "Show all resting orders.\n\n"
        "Usage:\n"
        "  /orders\n\n"
        "Lists limit orders, take-profit triggers, and stop-loss triggers\n"
        "with their order IDs. Use the ID with /cancel to remove a specific order."
    ),
    "price": (
        "Show the current mid price for an asset.\n\n"
        "Usage:\n"
        "  /price <coin>\n\n"
        "Examples:\n"
        "  /price BTC\n"
        "  /price ETH"
    ),
    "assets": (
        "List all tradeable perpetual assets with their max leverage.\n\n"
        "Usage:\n"
        "  /assets\n\n"
        "Use the symbol shown here with all other commands."
    ),
}

# ── helpers ───────────────────────────────────────────────────────────────────

def _get_client(tg_id: int) -> HLClient:
    creds = db.get_user(tg_id)
    if not creds:
        raise ValueError(
            "You are not registered yet.\n"
            "Send /register <agent_key> <account_address>\n\n"
            "Need help? Send /help register"
        )
    key = (tg_id, os.getenv("NETWORK", "mainnet"))
    if key not in _clients:
        _clients[key] = HLClient(
            private_key=creds["agent_key"],
            account_address=creds["account_address"],
        )
    return _clients[key]


async def _guard(update: Update) -> bool:
    if update.effective_user.id not in ALLOWED_IDS:
        await update.message.reply_text("Access denied.")
        return False
    return True


def _store_pending(tg_id: int, preview: str, fn):
    _pending[tg_id] = {"fn": fn, "preview": preview, "expires": time.time() + CONFIRM_TTL}


def _pop_pending(tg_id: int) -> Optional[dict]:
    entry = _pending.pop(tg_id, None)
    if entry is None:
        return None
    if time.time() > entry["expires"]:
        return None  # expired
    return entry


def _fmt_result(result: dict) -> str:
    if result.get("status") != "ok":
        return f"❌ Exchange error: {result}"
    lines = []
    response_type = result.get("response", {}).get("type")
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    for s in statuses:
        if response_type == "cancel" or s == "success":
            lines.append("✅ Order cancelled")
        elif isinstance(s, dict):
            if "filled" in s:
                f = s["filled"]
                lines.append(f"✅ Filled\n   Avg price: ${float(f.get('avgPx', 0)):,.2f}\n   Size: {f.get('totalSz')}")
            elif "resting" in s:
                lines.append(f"⏳ Order resting on book\n   ID: {s['resting'].get('oid')}\n   (view with /orders)")
            elif "error" in s:
                lines.append(f"❌ {s['error']}")
    return "\n".join(lines) or "Done."


def _fmt_positions(state: dict, prices: dict, spot_usdc: float) -> str:
    positions = [
        e for e in state.get("assetPositions", [])
        if float(e["position"]["szi"]) != 0
    ]
    lines = ["📊 *Open Positions*\n"]
    if not positions:
        lines = ["📭 No open positions."]
    else:
        for e in positions:
            pos = e["position"]
            size = float(pos["szi"])
            is_long = size > 0
            side_emoji = "🟢" if is_long else "🔴"
            side = "LONG" if is_long else "SHORT"
            pnl = float(pos.get("unrealizedPnl", 0))
            pnl_str = f"+${pnl:.2f} 📈" if pnl >= 0 else f"-${abs(pnl):.2f} 📉"
            mark = prices.get(pos["coin"])
            mark_str = f"${float(mark):,.2f}" if mark else "—"
            liq = pos.get("liquidationPx")
            liq_str = f"\n   Liq:      ${float(liq):,.2f} ⚠️" if liq else ""
            lev = pos.get("leverage", {})
            lines.append(
                f"{side_emoji} *{pos['coin']} {side}*\n"
                f"   Size:     {abs(size)}\n"
                f"   Entry:    ${float(pos['entryPx']):,.2f}\n"
                f"   Mark:     {mark_str}\n"
                f"   PnL:      {pnl_str}\n"
                f"   Leverage: {lev.get('value')}x {lev.get('type')}"
                f"{liq_str}"
            )

    summary = state.get("marginSummary", {})
    perp = float(summary.get("accountValue", 0))
    total = perp + spot_usdc
    lines.append(
        f"\n💰 *Balance*\n"
        f"   Perp:  ${perp:,.2f}\n"
        f"   Spot:  ${spot_usdc:,.2f}\n"
        f"   Total: ${total:,.2f}"
    )
    return "\n".join(lines)


def _fmt_orders(orders: list) -> str:
    if not orders:
        return "📭 No open orders."
    lines = ["📋 *Open Orders*\n"]
    for o in orders:
        side = "BUY" if o.get("side") == "B" else "SELL"
        side_emoji = "🟢" if side == "BUY" else "🔴"
        lines.append(
            f"{side_emoji} *{o.get('coin')}* {side}\n"
            f"   Type:  {o.get('orderType')}\n"
            f"   Size:  {o.get('sz')}\n"
            f"   Price: ${float(o.get('limitPx', 0)):,.2f}\n"
            f"   ID:    `{o.get('oid')}`"
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
            "Quick commands:\n"
            "/positions — your open positions\n"
            "/orders — resting orders\n"
            "/price BTC — current price\n\n"
            "Need help? /help or /help <command>"
        )
    else:
        await update.message.reply_text(
            f"👋 Hey {name}! Welcome to hl\\-trade bot\\.\n\n"
            "This bot lets you trade Hyperliquid perpetuals from your phone\\.\n\n"
            "*Setup \\(one time\\):*\n"
            "1\\. Generate an agent wallet — it can trade but cannot withdraw funds\n"
            "2\\. Approve it at app\\.hyperliquid\\.xyz → Settings → API\n"
            "3\\. Send /register with your keys\n\n"
            "📖 Full setup guide: /help register\n\n"
            "Once registered all commands become available\\.",
            parse_mode="MarkdownV2"
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if context.args:
        cmd = context.args[0].lower().lstrip("/")
        text = _HELP.get(cmd)
        if text:
            await update.message.reply_text(f"📖 /{cmd}\n\n{text}")
        else:
            await update.message.reply_text(
                f"No help for '{cmd}'.\n\n"
                "Available: " + ", ".join(f"/{k}" for k in _HELP)
            )
    else:
        await update.message.reply_text(
            "📖 *Commands*\n\n"
            "*Account*\n"
            "/positions — open positions \\& balance\n"
            "/orders — resting orders\n"
            "/price \\<coin\\> — current price\n"
            "/assets — available markets\n\n"
            "*Trading*\n"
            "/open \\<coin\\> \\<long\\|short\\> \\<size\\> \\<leverage\\> \\[tp\\] \\[sl\\]\n"
            "/close \\<coin\\> \\[size\\]\n"
            "/tp \\<coin\\> \\<price\\> \\[size\\]\n"
            "/sl \\<coin\\> \\<price\\> \\[size\\]\n"
            "/cancel \\<coin\\> \\<tp\\|sl\\|order\\_id\\>\n\n"
            "*Confirm/dismiss pending trades*\n"
            "/confirm — execute the previewed order\n"
            "/dismiss — discard it\n\n"
            "Type /help \\<command\\> for detailed help and examples\\.",
            parse_mode="MarkdownV2"
        )


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "Usage: /register <agent_key> <account_address>\n\n"
            "Both start with 0x.\n\n"
            "📖 Full guide: /help register"
        )
        return
    agent_key, account_address = args
    if not agent_key.startswith("0x") or not account_address.startswith("0x"):
        await update.message.reply_text(
            "❌ Both values must start with 0x.\n\n"
            "Example:\n"
            "/register 0xabc...def 0x123...456"
        )
        return
    try:
        db.register_user(update.effective_user.id, agent_key, account_address)
        _clients.pop((update.effective_user.id, os.getenv("NETWORK", "mainnet")), None)
        await update.message.reply_text(
            "✅ Registered successfully!\n\n"
            "⚠️ Please delete your /register message now — it contains your private key.\n\n"
            "Try /positions to verify your account is connected."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Registration failed: {e}")


async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    entry = _pop_pending(update.effective_user.id)
    if not entry:
        await update.message.reply_text(
            "Nothing to confirm — or the order preview expired (60s timeout).\n\n"
            "Use /open, /close, /tp, or /sl to create a new order preview."
        )
        return
    try:
        result = entry["fn"]()
        await update.message.reply_text(_fmt_result(result))
    except Exception as e:
        await update.message.reply_text(f"❌ Order failed: {e}")


async def cmd_dismiss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if _pending.pop(update.effective_user.id, None):
        await update.message.reply_text("🚫 Order dismissed.")
    else:
        await update.message.reply_text("Nothing to dismiss.")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    try:
        client = _get_client(update.effective_user.id)
        state = client.get_positions()
        prices = client.get_prices()
        spot = client.get_spot_usdc()
        await update.message.reply_text(
            _fmt_positions(state, prices, spot),
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            "Usage: /open <coin> <long|short> <size> <leverage> [tp] [sl]\n\n"
            "Examples:\n"
            "  /open BTC long 0.001 10\n"
            "  /open BTC long 0.001 10 75000 55000\n\n"
            "📖 /help open"
        )
        return
    try:
        coin = args[0].upper()
        side = args[1].lower()
        if side not in ("long", "short"):
            await update.message.reply_text("❌ Side must be 'long' or 'short'.")
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
        direction = "LONG 📈" if is_buy else "SHORT 📉"

        extras = ""
        if tp:
            extras += f"\n   Take-profit: ${tp:,.2f}"
        if sl:
            extras += f"\n   Stop-loss:   ${sl:,.2f}"

        preview = (
            f"📋 *Order Preview*\n\n"
            f"{'🟢' if is_buy else '🔴'} *{coin} {direction}*\n"
            f"   Size:      {size} {coin}\n"
            f"   Price:     ~${price:,.2f}\n"
            f"   Notional:  ~${notional:,.2f}\n"
            f"   Leverage:  {leverage}x\n"
            f"   Margin:    ~${margin:,.2f}"
            f"{extras}\n\n"
            f"Send /confirm to execute or /dismiss to cancel.\n"
            f"⏱ Expires in {CONFIRM_TTL}s."
        )

        _store_pending(
            update.effective_user.id,
            preview,
            lambda: client.open_position(
                coin=coin, is_buy=is_buy, size=size,
                leverage=leverage, tp=tp, sl=sl,
            ),
        )
        await update.message.reply_text(preview, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}\n\n📖 /help open")


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /close <coin> [size]\n\n"
            "Examples:\n"
            "  /close BTC\n"
            "  /close BTC 0.0005\n\n"
            "📖 /help close"
        )
        return
    try:
        coin = args[0].upper()
        size = float(args[1]) if len(args) > 1 else None

        client = _get_client(update.effective_user.id)
        pos = client._find_position(coin)
        if not pos:
            await update.message.reply_text(
                f"❌ No open position for {coin}.\n\n"
                "Check your positions with /positions"
            )
            return

        pos_size = float(pos["szi"])
        close_size = size if size is not None else abs(pos_size)
        is_long = pos_size > 0
        price = client.get_mid_price(coin)
        pnl = (price - float(pos["entryPx"])) * close_size * (1 if is_long else -1)
        pnl_str = f"+${pnl:.2f} 📈" if pnl >= 0 else f"-${abs(pnl):.2f} 📉"
        size_label = f"{close_size} (full)" if size is None else f"{close_size} (partial)"

        preview = (
            f"📋 *Order Preview*\n\n"
            f"{'🟢' if is_long else '🔴'} *Close {coin} {'LONG' if is_long else 'SHORT'}*\n"
            f"   Size:    {size_label}\n"
            f"   Price:   ~${price:,.2f}\n"
            f"   Est PnL: {pnl_str}\n\n"
            f"Send /confirm to execute or /dismiss to cancel.\n"
            f"⏱ Expires in {CONFIRM_TTL}s."
        )

        _store_pending(
            update.effective_user.id,
            preview,
            lambda: client.close_position(coin=coin, size=size),
        )
        await update.message.reply_text(preview, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}\n\n📖 /help close")


async def cmd_tp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /tp <coin> <price> [size]\n\n"
            "Example: /tp BTC 75000\n\n"
            "📖 /help tp"
        )
        return
    try:
        coin = args[0].upper()
        price = float(args[1])
        size = float(args[2]) if len(args) > 2 else None

        client = _get_client(update.effective_user.id)
        pos = client._find_position(coin)
        if not pos:
            await update.message.reply_text(
                f"❌ No open position for {coin}.\n\n"
                "Open a position first with /open"
            )
            return

        is_long = float(pos["szi"]) > 0
        entry = float(pos["entryPx"])
        close_size = size or abs(float(pos["szi"]))
        pnl = (price - entry) * close_size * (1 if is_long else -1)
        pnl_str = f"+${pnl:.2f} 📈" if pnl >= 0 else f"-${abs(pnl):.2f} 📉"

        if (is_long and price <= entry) or (not is_long and price >= entry):
            warning = "\n⚠️ Warning: TP price is not profitable vs your entry."
        else:
            warning = ""

        preview = (
            f"📋 *Take-Profit Preview*\n\n"
            f"{'🟢' if is_long else '🔴'} *{coin} {'LONG' if is_long else 'SHORT'}*\n"
            f"   Entry:     ${entry:,.2f}\n"
            f"   TP Trigger: ${price:,.2f}\n"
            f"   Size:      {close_size}\n"
            f"   Est PnL:   {pnl_str}"
            f"{warning}\n\n"
            f"Send /confirm to set or /dismiss to cancel.\n"
            f"⏱ Expires in {CONFIRM_TTL}s."
        )

        _store_pending(
            update.effective_user.id,
            preview,
            lambda: client.set_tp(coin=coin, trigger_price=price, size=size),
        )
        await update.message.reply_text(preview, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}\n\n📖 /help tp")


async def cmd_sl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /sl <coin> <price> [size]\n\n"
            "Example: /sl BTC 55000\n\n"
            "📖 /help sl"
        )
        return
    try:
        coin = args[0].upper()
        price = float(args[1])
        size = float(args[2]) if len(args) > 2 else None

        client = _get_client(update.effective_user.id)
        pos = client._find_position(coin)
        if not pos:
            await update.message.reply_text(
                f"❌ No open position for {coin}.\n\n"
                "Open a position first with /open"
            )
            return

        is_long = float(pos["szi"]) > 0
        entry = float(pos["entryPx"])
        close_size = size or abs(float(pos["szi"]))
        loss = abs((price - entry) * close_size)

        if (is_long and price >= entry) or (not is_long and price <= entry):
            warning = "\n⚠️ Warning: SL price is above your entry — it would lock in a profit, not a loss."
        else:
            warning = ""

        preview = (
            f"📋 *Stop-Loss Preview*\n\n"
            f"{'🟢' if is_long else '🔴'} *{coin} {'LONG' if is_long else 'SHORT'}*\n"
            f"   Entry:      ${entry:,.2f}\n"
            f"   SL Trigger: ${price:,.2f}\n"
            f"   Size:       {close_size}\n"
            f"   Max loss:   -${loss:.2f}"
            f"{warning}\n\n"
            f"Send /confirm to set or /dismiss to cancel.\n"
            f"⏱ Expires in {CONFIRM_TTL}s."
        )

        _store_pending(
            update.effective_user.id,
            preview,
            lambda: client.set_sl(coin=coin, trigger_price=price, size=size),
        )
        await update.message.reply_text(preview, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}\n\n📖 /help sl")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /cancel <coin> <tp|sl|order_id>\n\n"
            "Examples:\n"
            "  /cancel BTC tp\n"
            "  /cancel BTC sl\n"
            "  /cancel BTC 54479543383\n\n"
            "📖 /help cancel"
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
                await update.message.reply_text(
                    f"✅ Cancelled {len(results)} {target.upper()} order(s) for {coin}."
                )
        else:
            result = client.cancel_by_id(coin=coin, oid=int(target))
            await update.message.reply_text(_fmt_result(result))
    except Exception as e:
        await update.message.reply_text(f"❌ {e}\n\n📖 /help cancel")


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    try:
        orders = _get_client(update.effective_user.id).get_open_orders()
        await update.message.reply_text(_fmt_orders(orders), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /price <coin>\nExample: /price BTC")
        return
    try:
        coin = context.args[0].upper()
        price = _get_client(update.effective_user.id).get_mid_price(coin)
        await update.message.reply_text(f"💲 {coin}: ${price:,.4f}")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_assets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    try:
        assets = _get_client(update.effective_user.id).get_assets()
        lines = ["📈 *Available Perpetuals*\n"]
        for a in assets:
            lines.append(f"{a['name']} — max {a.get('maxLeverage', '?')}x")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN not set in .env")
    db.init_db()

    app = Application.builder().token(TOKEN).build()
    for cmd, handler in [
        ("start",     cmd_start),
        ("help",      cmd_help),
        ("register",  cmd_register),
        ("confirm",   cmd_confirm),
        ("dismiss",   cmd_dismiss),
        ("positions", cmd_positions),
        ("open",      cmd_open),
        ("close",     cmd_close),
        ("tp",        cmd_tp),
        ("sl",        cmd_sl),
        ("cancel",    cmd_cancel),
        ("orders",    cmd_orders),
        ("price",     cmd_price),
        ("assets",    cmd_assets),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    logging.info("Bot started. Polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
