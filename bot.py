import asyncio
import logging
import os
from typing import Optional

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import db
from client import HLClient

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)

TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_IDS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_TG_IDS", "").split(",")
    if x.strip()
}

# Per-user HLClient cache keyed by (tg_id, network)
_clients: dict[tuple, HLClient] = {}


def _get_client(tg_id: int) -> HLClient:
    creds = db.get_user(tg_id)
    if not creds:
        raise ValueError("Not registered. Send /register <agent_key> <account_address>")
    key = (tg_id, os.getenv("NETWORK", "mainnet"))
    if key not in _clients:
        _clients[key] = HLClient(
            private_key=creds["agent_key"],
            account_address=creds["account_address"],
        )
    return _clients[key]


async def _check_access(update: Update) -> bool:
    if update.effective_user.id not in ALLOWED_IDS:
        await update.message.reply_text("Access denied.")
        return False
    return True


def _fmt_result(result: dict) -> str:
    if result.get("status") != "ok":
        return f"Exchange error: {result}"
    lines = []
    response_type = result.get("response", {}).get("type")
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    for s in statuses:
        if response_type == "cancel" or s == "success":
            lines.append("✓ Cancelled")
        elif isinstance(s, dict):
            if "filled" in s:
                f = s["filled"]
                lines.append(f"✓ Filled  avg ${float(f.get('avgPx', 0)):,.2f}  size {f.get('totalSz')}")
            elif "resting" in s:
                lines.append(f"⟳ Resting  order ID: {s['resting'].get('oid')}")
            elif "error" in s:
                lines.append(f"✗ {s['error']}")
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
            side = "LONG" if size > 0 else "SHORT"
            pnl = float(pos.get("unrealizedPnl", 0))
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            mark = prices.get(pos["coin"])
            mark_str = f"${float(mark):,.2f}" if mark else "-"
            lev = pos.get("leverage", {})
            lines.append(
                f"{pos['coin']} {side} | {abs(size)} | "
                f"entry ${float(pos['entryPx']):,.2f} | mark {mark_str} | "
                f"PnL {pnl_str} | {lev.get('value')}x {lev.get('type')}"
            )

    summary = state.get("marginSummary", {})
    perp = float(summary.get("accountValue", 0))
    total = perp + spot_usdc
    lines.append(f"\nPerp: ${perp:,.2f}  Spot: ${spot_usdc:,.2f}  Total: ${total:,.2f}")
    return "\n".join(lines)


# ── commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update):
        return
    await update.message.reply_text(
        "hl\\-trade bot\n\n"
        "Register once:\n"
        "/register <agent\\_key> <account\\_address>\n\n"
        "Commands:\n"
        "/positions\n"
        "/open <coin> <long|short> <size> <leverage> [tp] [sl]\n"
        "/close <coin> [size]\n"
        "/tp <coin> <price> [size]\n"
        "/sl <coin> <price> [size]\n"
        "/cancel <coin> <tp|sl|order\\_id>\n"
        "/orders\n"
        "/price <coin>\n"
        "/assets",
        parse_mode="MarkdownV2",
    )


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update):
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "Usage: /register <agent_key> <account_address>\n"
            "Both values start with 0x."
        )
        return
    agent_key, account_address = args
    if not agent_key.startswith("0x") or not account_address.startswith("0x"):
        await update.message.reply_text("Both values must start with 0x.")
        return
    try:
        db.register_user(update.effective_user.id, agent_key, account_address)
        # Invalidate cached client so next command picks up new creds
        _clients.pop((update.effective_user.id, os.getenv("NETWORK", "mainnet")), None)
        await update.message.reply_text(
            "✓ Registered. Try /positions\n\n"
            "⚠️ Delete your /register message now — it contains your private key."
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update):
        return
    try:
        client = _get_client(update.effective_user.id)
        state = client.get_positions()
        prices = client.get_prices()
        spot = client.get_spot_usdc()
        await update.message.reply_text(_fmt_positions(state, prices, spot))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update):
        return
    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            "Usage: /open <coin> <long|short> <size> <leverage> [tp_price] [sl_price]"
        )
        return
    try:
        coin, side = args[0].upper(), args[1].lower()
        size, leverage = float(args[2]), int(args[3])
        tp = float(args[4]) if len(args) > 4 else None
        sl = float(args[5]) if len(args) > 5 else None
        if side not in ("long", "short"):
            await update.message.reply_text("Side must be 'long' or 'short'.")
            return
        result = _get_client(update.effective_user.id).open_position(
            coin=coin, is_buy=side == "long", size=size,
            leverage=leverage, tp=tp, sl=sl,
        )
        await update.message.reply_text(_fmt_result(result))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /close <coin> [size]")
        return
    try:
        coin = args[0].upper()
        size = float(args[1]) if len(args) > 1 else None
        result = _get_client(update.effective_user.id).close_position(coin=coin, size=size)
        await update.message.reply_text(_fmt_result(result))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_tp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /tp <coin> <price> [size]")
        return
    try:
        coin, price = args[0].upper(), float(args[1])
        size = float(args[2]) if len(args) > 2 else None
        result = _get_client(update.effective_user.id).set_tp(coin=coin, trigger_price=price, size=size)
        await update.message.reply_text(_fmt_result(result))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_sl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /sl <coin> <price> [size]")
        return
    try:
        coin, price = args[0].upper(), float(args[1])
        size = float(args[2]) if len(args) > 2 else None
        result = _get_client(update.effective_user.id).set_sl(coin=coin, trigger_price=price, size=size)
        await update.message.reply_text(_fmt_result(result))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /cancel <coin> <tp|sl|order_id>")
        return
    try:
        coin, target = args[0].upper(), args[1]
        client = _get_client(update.effective_user.id)
        if target.lower() in ("tp", "sl"):
            results = client.cancel_tpsl(coin=coin, tpsl_type=target.lower())
            if not results:
                await update.message.reply_text(f"No {target.upper()} orders found for {coin}.")
            else:
                await update.message.reply_text(f"✓ Cancelled {len(results)} {target.upper()} order(s)")
        else:
            result = client.cancel_by_id(coin=coin, oid=int(target))
            await update.message.reply_text(_fmt_result(result))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update):
        return
    try:
        orders = _get_client(update.effective_user.id).get_open_orders()
        if not orders:
            await update.message.reply_text("No open orders.")
            return
        lines = []
        for o in orders:
            side = "BUY" if o.get("side") == "B" else "SELL"
            lines.append(
                f"{o.get('coin')} {side} | {o.get('orderType')} | "
                f"size {o.get('sz')} | ${float(o.get('limitPx', 0)):,.2f} | "
                f"ID {o.get('oid')}"
            )
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /price <coin>")
        return
    try:
        coin = context.args[0].upper()
        price = _get_client(update.effective_user.id).get_mid_price(coin)
        await update.message.reply_text(f"{coin}: ${price:,.4f}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_assets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_access(update):
        return
    try:
        assets = _get_client(update.effective_user.id).get_assets()
        lines = [f"{a['name']} — max {a.get('maxLeverage', '?')}x" for a in assets]
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN not set in .env")
    db.init_db()

    app = Application.builder().token(TOKEN).build()
    for cmd, handler in [
        ("start",     cmd_start),
        ("register",  cmd_register),
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
