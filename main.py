from typing import Optional
import typer
from client import HLClient
import display

app = typer.Typer(help="Hyperliquid perpetuals trading CLI")

_client: Optional[HLClient] = None


def get_client() -> HLClient:
    global _client
    if _client is None:
        _client = HLClient()
    return _client


def handle_error(e: Exception):
    typer.echo(f"Error: {e}", err=True)
    raise typer.Exit(1)


@app.command("open")
def open_cmd(
    coin: str = typer.Option(..., "--coin", "-c", help="Asset symbol, e.g. BTC"),
    side: str = typer.Option(..., "--side", "-s", help="'long' or 'short'"),
    size: float = typer.Option(..., "--size", help="Position size in base asset units"),
    leverage: int = typer.Option(1, "--leverage", "-l", help="Leverage multiplier"),
    limit: Optional[float] = typer.Option(None, "--limit", help="Limit price (omit for market order)"),
    cross: bool = typer.Option(True, "--cross/--isolated", help="Cross or isolated margin"),
    tp: Optional[float] = typer.Option(None, "--tp", help="Take-profit trigger price"),
    sl: Optional[float] = typer.Option(None, "--sl", help="Stop-loss trigger price"),
):
    """Open a long or short position."""
    side_lower = side.lower()
    if side_lower not in ("long", "short"):
        typer.echo("--side must be 'long' or 'short'", err=True)
        raise typer.Exit(1)
    try:
        result = get_client().open_position(
            coin=coin.upper(),
            is_buy=side_lower == "long",
            size=size,
            leverage=leverage,
            limit_px=limit,
            is_cross=cross,
            tp=tp,
            sl=sl,
        )
        display.show_result(result)
    except Exception as e:
        handle_error(e)


@app.command("close")
def close_cmd(
    coin: str = typer.Option(..., "--coin", "-c", help="Asset symbol"),
    size: Optional[float] = typer.Option(None, "--size", help="Size to close (omit to close full position)"),
    limit: Optional[float] = typer.Option(None, "--limit", help="Limit price (omit for market order)"),
):
    """Close an open position fully or partially."""
    try:
        result = get_client().close_position(coin=coin.upper(), size=size, limit_px=limit)
        display.show_result(result)
    except Exception as e:
        handle_error(e)


@app.command("tp")
def tp_cmd(
    coin: str = typer.Option(..., "--coin", "-c", help="Asset symbol"),
    price: float = typer.Option(..., "--price", "-p", help="Trigger price"),
    size: Optional[float] = typer.Option(None, "--size", help="Size (omit to use full position size)"),
):
    """Set a take-profit order on an open position."""
    try:
        result = get_client().set_tp(coin=coin.upper(), trigger_price=price, size=size)
        display.show_result(result)
    except Exception as e:
        handle_error(e)


@app.command("sl")
def sl_cmd(
    coin: str = typer.Option(..., "--coin", "-c", help="Asset symbol"),
    price: float = typer.Option(..., "--price", "-p", help="Trigger price"),
    size: Optional[float] = typer.Option(None, "--size", help="Size (omit to use full position size)"),
):
    """Set a stop-loss order on an open position."""
    try:
        result = get_client().set_sl(coin=coin.upper(), trigger_price=price, size=size)
        display.show_result(result)
    except Exception as e:
        handle_error(e)


@app.command("cancel")
def cancel_cmd(
    coin: str = typer.Option(..., "--coin", "-c", help="Asset symbol"),
    type_: str = typer.Option(..., "--type", "-t", help="Order type to cancel: 'tp' or 'sl'"),
):
    """Cancel existing TP or SL orders for a coin."""
    if type_.lower() not in ("tp", "sl"):
        typer.echo("--type must be 'tp' or 'sl'", err=True)
        raise typer.Exit(1)
    try:
        results = get_client().cancel_tpsl(coin=coin.upper(), tpsl_type=type_.lower())
        if not results:
            typer.echo(f"No {type_.upper()} orders found for {coin.upper()}.")
        for r in results:
            display.show_result(r)
    except Exception as e:
        handle_error(e)


@app.command("positions")
def positions_cmd():
    """Show all open positions and account summary."""
    try:
        client = get_client()
        state = client.get_positions()
        prices = client.get_prices()
        display.show_positions(state, prices)
    except Exception as e:
        handle_error(e)


@app.command("orders")
def orders_cmd():
    """Show all open orders (limits, TP, SL)."""
    try:
        orders = get_client().get_open_orders()
        display.show_orders(orders)
    except Exception as e:
        handle_error(e)


@app.command("price")
def price_cmd(
    coin: str = typer.Argument(..., help="Asset symbol, e.g. BTC"),
):
    """Show current mid price for an asset."""
    try:
        mid = get_client().get_mid_price(coin.upper())
        typer.echo(f"{coin.upper()}: ${mid:,.4f}")
    except Exception as e:
        handle_error(e)


@app.command("assets")
def assets_cmd():
    """List all available perpetual assets."""
    try:
        universe = get_client().get_assets()
        display.show_assets(universe)
    except Exception as e:
        handle_error(e)


if __name__ == "__main__":
    app()
