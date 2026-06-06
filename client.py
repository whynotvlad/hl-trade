from typing import Optional
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
import config

MARKET_SLIPPAGE = 0.02  # 2% slippage used to simulate market orders via IOC limit


class HLClient:
    def __init__(self):
        config.validate()
        self._base_url = config.get_base_url()
        self._account = Account.from_key(config.PRIVATE_KEY)
        self.address = self._account.address.lower()
        self.info = Info(self._base_url, skip_ws=True)
        self.exchange = Exchange(self._account, self._base_url)
        self._universe: Optional[list] = None

    # --- meta / market data ---

    def _load_universe(self):
        if self._universe is None:
            meta = self.info.meta()
            self._universe = meta["universe"]

    def get_assets(self) -> list:
        self._load_universe()
        return self._universe

    def get_prices(self) -> dict:
        return self.info.all_mids()

    def get_mid_price(self, coin: str) -> float:
        prices = self.get_prices()
        if coin not in prices:
            raise ValueError(f"Unknown coin '{coin}'. Run `assets` to list available symbols.")
        return float(prices[coin])

    # --- account state ---

    def get_positions(self) -> dict:
        return self.info.user_state(self.address)

    def get_open_orders(self) -> list:
        return self.info.frontend_open_orders(self.address)

    def _find_position(self, coin: str) -> Optional[dict]:
        state = self.info.user_state(self.address)
        for entry in state.get("assetPositions", []):
            pos = entry["position"]
            if pos["coin"] == coin and float(pos["szi"]) != 0:
                return pos
        return None

    # --- order helpers ---

    def _ioc_price(self, coin: str, is_buy: bool) -> float:
        mid = self.get_mid_price(coin)
        # IOC far enough from mid to fill immediately in normal conditions
        return round(mid * (1 + MARKET_SLIPPAGE) if is_buy else mid * (1 - MARKET_SLIPPAGE), 6)

    # --- trading actions ---

    def open_position(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        leverage: int,
        limit_px: Optional[float] = None,
        is_cross: bool = True,
        tp: Optional[float] = None,
        sl: Optional[float] = None,
    ) -> dict:
        self.exchange.update_leverage(leverage, coin, is_cross=is_cross)

        if limit_px is None:
            price = self._ioc_price(coin, is_buy)
            order_type = {"limit": {"tif": "Ioc"}}
        else:
            price = limit_px
            order_type = {"limit": {"tif": "Gtc"}}

        result = self.exchange.order(coin, is_buy, size, price, order_type)

        if tp is not None:
            self.set_tp(coin, tp, size, is_long=is_buy)
        if sl is not None:
            self.set_sl(coin, sl, size, is_long=is_buy)

        return result

    def close_position(
        self,
        coin: str,
        size: Optional[float] = None,
        limit_px: Optional[float] = None,
    ) -> dict:
        pos = self._find_position(coin)
        if pos is None:
            raise ValueError(f"No open position for {coin}.")

        pos_size = float(pos["szi"])
        is_long = pos_size > 0
        close_size = size if size is not None else abs(pos_size)
        is_buy = not is_long  # sell to close long, buy to close short

        if limit_px is None:
            price = self._ioc_price(coin, is_buy)
            order_type = {"limit": {"tif": "Ioc"}}
        else:
            price = limit_px
            order_type = {"limit": {"tif": "Gtc"}}

        return self.exchange.order(coin, is_buy, close_size, price, order_type, reduce_only=True)

    def set_tp(
        self,
        coin: str,
        trigger_price: float,
        size: Optional[float] = None,
        is_long: Optional[bool] = None,
    ) -> dict:
        if is_long is None or size is None:
            pos = self._find_position(coin)
            if pos is None:
                raise ValueError(f"No open position for {coin}.")
            if is_long is None:
                is_long = float(pos["szi"]) > 0
            if size is None:
                size = abs(float(pos["szi"]))

        is_buy = not is_long  # TP for long = sell when price goes up
        order_type = {"trigger": {"triggerPx": trigger_price, "isMarket": True, "tpsl": "tp"}}
        return self.exchange.order(coin, is_buy, size, trigger_price, order_type, reduce_only=True)

    def set_sl(
        self,
        coin: str,
        trigger_price: float,
        size: Optional[float] = None,
        is_long: Optional[bool] = None,
    ) -> dict:
        if is_long is None or size is None:
            pos = self._find_position(coin)
            if pos is None:
                raise ValueError(f"No open position for {coin}.")
            if is_long is None:
                is_long = float(pos["szi"]) > 0
            if size is None:
                size = abs(float(pos["szi"]))

        is_buy = not is_long  # SL for long = sell when price drops
        order_type = {"trigger": {"triggerPx": trigger_price, "isMarket": True, "tpsl": "sl"}}
        return self.exchange.order(coin, is_buy, size, trigger_price, order_type, reduce_only=True)

    def cancel_tpsl(self, coin: str, tpsl_type: str) -> list:
        """Cancel all TP or SL orders for a given coin. tpsl_type: 'tp' or 'sl'."""
        type_label = "Take Profit Market" if tpsl_type == "tp" else "Stop Market"
        orders = self.info.frontend_open_orders(self.address)
        results = []
        for order in orders:
            if order.get("coin") == coin and order.get("orderType") == type_label:
                results.append(self.exchange.cancel(coin, order["oid"]))
        return results
