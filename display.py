from rich.console import Console
from rich.table import Table

console = Console()


def show_positions(state: dict, prices: dict, spot_usdc: float = 0.0):
    positions = [
        e for e in state.get("assetPositions", [])
        if float(e["position"]["szi"]) != 0
    ]

    if not positions:
        console.print("[yellow]No open positions.[/yellow]")
    else:
        table = Table(title="Open Positions", show_lines=True)
        table.add_column("Coin", style="cyan bold")
        table.add_column("Side")
        table.add_column("Size", justify="right")
        table.add_column("Entry Price", justify="right")
        table.add_column("Mark Price", justify="right")
        table.add_column("Liq. Price", justify="right")
        table.add_column("Unrealized PnL", justify="right")
        table.add_column("Leverage")

        for entry in positions:
            pos = entry["position"]
            size = float(pos["szi"])
            is_long = size > 0
            side = "[green]LONG[/green]" if is_long else "[red]SHORT[/red]"
            pnl = float(pos.get("unrealizedPnl", 0))
            pnl_str = f"[green]+{pnl:.2f}[/green]" if pnl >= 0 else f"[red]{pnl:.2f}[/red]"
            mark = prices.get(pos["coin"])
            mark_str = f"${float(mark):,.2f}" if mark else "-"
            liq = pos.get("liquidationPx")
            liq_str = f"${float(liq):,.2f}" if liq else "-"
            lev = pos.get("leverage", {})
            lev_str = f"{lev.get('value', '?')}x {lev.get('type', '')}"

            table.add_row(
                pos["coin"],
                side,
                str(abs(size)),
                f"${float(pos['entryPx']):,.2f}",
                mark_str,
                liq_str,
                pnl_str,
                lev_str,
            )

        console.print(table)

    summary = state.get("marginSummary", {})
    perp_value = float(summary.get("accountValue", 0))
    total = perp_value + spot_usdc
    console.print(
        f"\n  Perp account:      [bold]${perp_value:,.2f}[/bold]\n"
        f"  Spot USDC:         ${spot_usdc:,.2f}\n"
        f"  Total:             [bold]${total:,.2f}[/bold]\n"
        f"  Margin used:       ${float(summary.get('totalMarginUsed', 0)):,.2f}\n"
        f"  Withdrawable:      ${float(state.get('withdrawable', 0)):,.2f}"
    )


def show_orders(orders: list):
    if not orders:
        console.print("[yellow]No open orders.[/yellow]")
        return

    table = Table(title="Open Orders", show_lines=True)
    table.add_column("Coin", style="cyan bold")
    table.add_column("Side")
    table.add_column("Type")
    table.add_column("Size", justify="right")
    table.add_column("Limit Price", justify="right")
    table.add_column("Order ID", style="dim")

    for order in orders:
        side = "[green]BUY[/green]" if order.get("side") == "B" else "[red]SELL[/red]"
        table.add_row(
            order.get("coin", "-"),
            side,
            order.get("orderType", "Limit"),
            str(order.get("sz", "-")),
            f"${float(order.get('limitPx', 0)):,.4f}",
            str(order.get("oid", "-")),
        )

    console.print(table)


def show_assets(universe: list):
    table = Table(title="Available Perpetuals")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Symbol", style="cyan bold")
    table.add_column("Max Leverage", justify="right")

    for i, asset in enumerate(universe):
        table.add_row(str(i), asset["name"], f"{asset.get('maxLeverage', '?')}x")

    console.print(table)


def show_result(result: dict):
    if result is None:
        console.print("[red]No response from exchange.[/red]")
        return

    if result.get("status") != "ok":
        console.print(f"[red]Exchange error:[/red] {result}")
        return

    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    for s in statuses:
        if "filled" in s:
            f = s["filled"]
            console.print(
                f"[green]✓ Filled[/green]  avg ${float(f.get('avgPx', 0)):,.4f}"
                f"  size {f.get('totalSz', '?')}"
            )
        elif "resting" in s:
            console.print(f"[yellow]⟳ Resting[/yellow]  order ID: {s['resting'].get('oid')}")
        elif "error" in s:
            console.print(f"[red]✗ Order error:[/red] {s['error']}")
