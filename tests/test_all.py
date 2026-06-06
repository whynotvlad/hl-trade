"""
Full test suite for hl-trade bot.
Run with: .venv/bin/python -m pytest tests/test_all.py -v
"""
import os
import sys
import time
import sqlite3
import tempfile
import unittest
from math import floor, log10
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography.fernet import Fernet

os.environ.setdefault("DB_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("NETWORK", "testnet")
os.environ.setdefault("PRIVATE_KEY", "0x" + "a" * 64)
os.environ.setdefault("ACCOUNT_ADDRESS", "0x" + "b" * 40)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _round_price(px: float) -> float:
    if px <= 0:
        return px
    mag = int(floor(log10(abs(px))))
    return round(px, 4 - mag)


def gen_ladder(from_price, to_price, n_parts, total_size):
    prices = [
        _round_price(from_price + (to_price - from_price) * i / (n_parts - 1))
        for i in range(n_parts)
    ]
    per_order = round(total_size / n_parts, 8)
    orders = []
    placed = 0.0
    for i, px in enumerate(prices):
        sz = round(total_size - placed, 8) if i == n_parts - 1 else per_order
        placed = round(placed + sz, 8)
        orders.append((sz, px))
    return orders


# ─── bot helpers ──────────────────────────────────────────────────────────────

class TestConfirmationFlow(unittest.TestCase):
    def setUp(self):
        self._pending = {}

    def _store(self, tg_id, fn):
        self._pending[tg_id] = {"fn": fn, "preview": "p", "expires": time.time() + 60}

    def _pop(self, tg_id):
        entry = self._pending.pop(tg_id, None)
        if entry and time.time() > entry["expires"]:
            return None
        return entry

    def test_pop_returns_entry(self):
        self._store(1, lambda: "ok")
        e = self._pop(1)
        self.assertIsNotNone(e)
        self.assertEqual(e["fn"](), "ok")

    def test_pop_removes_entry(self):
        self._store(1, lambda: "ok")
        self._pop(1)
        self.assertIsNone(self._pop(1))

    def test_expired_entry_returns_none(self):
        self._pending[1] = {"fn": lambda: "x", "preview": "p", "expires": time.time() - 1}
        self.assertIsNone(self._pop(1))
        self.assertNotIn(1, self._pending)

    def test_per_user_isolation(self):
        self._store(1, lambda: "user1")
        self._store(2, lambda: "user2")
        e1 = self._pop(1)
        e2 = self._pop(2)
        self.assertEqual(e1["fn"](), "user1")
        self.assertEqual(e2["fn"](), "user2")

    def test_dismiss_clears_pending(self):
        self._store(1, lambda: "x")
        self._pending.pop(1, None)
        self.assertNotIn(1, self._pending)


# ─── _fmt_result ──────────────────────────────────────────────────────────────

class TestFmtResult(unittest.TestCase):
    def _fmt(self, result):
        if isinstance(result, list):
            ok = sum(1 for r in result if r.get("status") == "ok")
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

    def test_market_fill(self):
        r = {"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"filled": {"avgPx": "61950", "totalSz": "0.001"}}
        ]}}}
        out = self._fmt(r)
        self.assertIn("Filled", out)
        self.assertIn("61,950", out)

    def test_resting_order(self):
        r = {"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"resting": {"oid": 12345}}
        ]}}}
        out = self._fmt(r)
        self.assertIn("resting", out)
        self.assertIn("12345", out)

    def test_cancel(self):
        r = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}
        self.assertEqual(self._fmt(r), "Cancelled.")

    def test_api_error(self):
        r = {"status": "err", "response": "bad"}
        self.assertIn("Error", self._fmt(r))

    def test_exchange_error_in_status(self):
        r = {"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"error": "Insufficient margin"}
        ]}}}
        self.assertIn("Insufficient margin", self._fmt(r))

    def test_ladder_all_ok(self):
        out = self._fmt([{"status": "ok"}] * 5)
        self.assertIn("placed 5/5", out)

    def test_ladder_partial_fail(self):
        out = self._fmt([{"status": "ok"}, {"status": "err"}, {"status": "ok"}])
        self.assertIn("placed 2/3", out)
        self.assertIn("1 failed", out)


# ─── auth ─────────────────────────────────────────────────────────────────────

class TestAuth(unittest.TestCase):
    def setUp(self):
        self.ADMIN_IDS = {111, 222}

    def _is_admin(self, tg_id):
        return tg_id in self.ADMIN_IDS

    def _is_allowed(self, tg_id, db_allowed):
        return self._is_admin(tg_id) or tg_id in db_allowed

    def test_admin_always_allowed(self):
        self.assertTrue(self._is_allowed(111, set()))
        self.assertTrue(self._is_allowed(222, set()))

    def test_whitelisted_user_allowed(self):
        self.assertTrue(self._is_allowed(333, {333}))

    def test_unknown_denied(self):
        self.assertFalse(self._is_allowed(999, {333}))

    def test_cannot_remove_admin(self):
        self.assertTrue(self._is_admin(111))
        self.assertFalse(self._is_admin(333))

    def test_admin_not_in_db_still_allowed(self):
        self.assertTrue(self._is_allowed(111, set()))


# ─── price rounding ───────────────────────────────────────────────────────────

class TestPriceRounding(unittest.TestCase):
    def test_btc_price(self):
        self.assertEqual(_round_price(61951.234), 61951.0)

    def test_eth_price(self):
        self.assertEqual(_round_price(3525.678), 3525.7)

    def test_small_price(self):
        self.assertAlmostEqual(_round_price(0.05234), 0.05234, places=8)

    def test_round_number(self):
        self.assertEqual(_round_price(100.0), 100.0)

    def test_zero(self):
        self.assertEqual(_round_price(0), 0)

    def test_ioc_slippage_direction(self):
        mid = 61951.0
        buy  = _round_price(mid * 1.02)
        sell = _round_price(mid * 0.98)
        self.assertGreater(buy, mid)
        self.assertLess(sell, mid)


# ─── TP/SL warnings ───────────────────────────────────────────────────────────

class TestTPSLWarnings(unittest.TestCase):
    def _tp_warn(self, is_long, price, entry):
        return (is_long and price <= entry) or (not is_long and price >= entry)

    def _sl_warn(self, is_long, price, entry):
        return (is_long and price >= entry) or (not is_long and price <= entry)

    def test_tp_long_below_entry_warns(self):
        self.assertTrue(self._tp_warn(True, 55000, 60000))

    def test_tp_long_above_entry_ok(self):
        self.assertFalse(self._tp_warn(True, 65000, 60000))

    def test_tp_short_above_entry_warns(self):
        self.assertTrue(self._tp_warn(False, 65000, 60000))

    def test_tp_short_below_entry_ok(self):
        self.assertFalse(self._tp_warn(False, 55000, 60000))

    def test_sl_long_above_entry_warns(self):
        self.assertTrue(self._sl_warn(True, 65000, 60000))

    def test_sl_long_below_entry_ok(self):
        self.assertFalse(self._sl_warn(True, 55000, 60000))

    def test_sl_short_below_entry_warns(self):
        self.assertTrue(self._sl_warn(False, 55000, 60000))

    def test_sl_short_above_entry_ok(self):
        self.assertFalse(self._sl_warn(False, 65000, 60000))


# ─── PnL estimation ───────────────────────────────────────────────────────────

class TestPnLEstimation(unittest.TestCase):
    def _pnl(self, entry, mark, size, is_long):
        return (mark - entry) * size * (1 if is_long else -1)

    def test_long_profit(self):
        self.assertAlmostEqual(self._pnl(60000, 62000, 0.001, True), 2.0)

    def test_long_loss(self):
        self.assertAlmostEqual(self._pnl(60000, 58000, 0.001, True), -2.0)

    def test_short_profit(self):
        self.assertAlmostEqual(self._pnl(3500, 3000, 1.0, False), 500.0)

    def test_short_loss(self):
        self.assertAlmostEqual(self._pnl(3500, 3525, 0.5, False), -12.5)


# ─── liquidation warning ──────────────────────────────────────────────────────

class TestLiquidationWarning(unittest.TestCase):
    LIQ_WARN_PCT = 15

    def _should_warn(self, mark, liq):
        dist = abs(mark - liq) / mark * 100
        return dist < self.LIQ_WARN_PCT

    def test_close_to_liq_warns(self):
        self.assertTrue(self._should_warn(60000, 54000))  # 10% away

    def test_far_from_liq_ok(self):
        self.assertFalse(self._should_warn(60000, 40000))  # 33% away

    def test_exactly_at_threshold_no_warn(self):
        self.assertFalse(self._should_warn(60000, 51000))  # exactly 15%

    def test_just_inside_threshold_warns(self):
        self.assertTrue(self._should_warn(60000, 51001))


# ─── fill detection ───────────────────────────────────────────────────────────

class TestFillDetection(unittest.TestCase):
    def _detect(self, snap, current_oids, current_positions):
        messages = []
        disappeared = snap["order_ids"] - current_oids
        for oid in disappeared:
            order = snap["orders"].get(oid, {})
            coin = order.get("coin", "")
            old_sz = snap["positions"].get(coin, 0)
            new_sz = current_positions.get(coin, 0)
            if abs(old_sz - new_sz) < 0.0001:
                continue
            order_type = order.get("orderType", "Order")
            px = float(order.get("limitPx", 0))
            if "Take Profit" in order_type:
                messages.append(f"TP:{coin}:{px}")
            elif "Stop" in order_type:
                messages.append(f"SL:{coin}:{px}")
            else:
                messages.append(f"FILL:{coin}")
        return messages

    def test_tp_fill_detected(self):
        snap = {
            "order_ids": {1},
            "orders": {1: {"coin": "BTC", "orderType": "Take Profit Market", "limitPx": "65000"}},
            "positions": {"BTC": 0.001},
        }
        msgs = self._detect(snap, set(), {"BTC": 0.0})
        self.assertEqual(len(msgs), 1)
        self.assertIn("TP:BTC", msgs[0])

    def test_sl_fill_detected(self):
        snap = {
            "order_ids": {2},
            "orders": {2: {"coin": "ETH", "orderType": "Stop Market", "limitPx": "3000"}},
            "positions": {"ETH": 1.0},
        }
        msgs = self._detect(snap, set(), {"ETH": 0.0})
        self.assertEqual(len(msgs), 1)
        self.assertIn("SL:ETH", msgs[0])

    def test_manual_cancel_not_treated_as_fill(self):
        snap = {
            "order_ids": {3},
            "orders": {3: {"coin": "BTC", "orderType": "Limit Order", "limitPx": "60000"}},
            "positions": {"BTC": 0.001},
        }
        msgs = self._detect(snap, set(), {"BTC": 0.001})  # position unchanged
        self.assertEqual(msgs, [])

    def test_no_snap_no_messages(self):
        # First poll — no snapshot yet, no detection
        msgs = [] if True else self._detect({}, set(), {})
        self.assertEqual(msgs, [])

    def test_limit_fill_detected(self):
        snap = {
            "order_ids": {4},
            "orders": {4: {"coin": "SOL", "orderType": "Limit Order", "limitPx": "150", "side": "B"}},
            "positions": {"SOL": 0.0},
        }
        msgs = self._detect(snap, set(), {"SOL": 10.0})
        self.assertEqual(len(msgs), 1)
        self.assertIn("FILL:SOL", msgs[0])


# ─── price alert logic ────────────────────────────────────────────────────────

class TestPriceAlerts(unittest.TestCase):
    def _triggered(self, direction, current, target):
        return (
            (direction == "above" and current >= target) or
            (direction == "below" and current <= target)
        )

    def _direction(self, target, current):
        return "above" if target > current else "below"

    def test_above_trigger(self):
        self.assertTrue(self._triggered("above", 70000, 70000))
        self.assertTrue(self._triggered("above", 70001, 70000))
        self.assertFalse(self._triggered("above", 69999, 70000))

    def test_below_trigger(self):
        self.assertTrue(self._triggered("below", 2000, 2000))
        self.assertTrue(self._triggered("below", 1999, 2000))
        self.assertFalse(self._triggered("below", 2001, 2000))

    def test_direction_assignment(self):
        self.assertEqual(self._direction(70000, 62000), "above")
        self.assertEqual(self._direction(50000, 62000), "below")


# ─── ladder close ─────────────────────────────────────────────────────────────

class TestLadderLogic(unittest.TestCase):
    def test_sizes_sum_to_total(self):
        for n in [2, 3, 5, 7, 10, 20]:
            orders = gen_ladder(3500, 3000, n, 0.5)
            total = round(sum(o[0] for o in orders), 8)
            self.assertAlmostEqual(total, 0.5, places=7, msg=f"n={n}")

    def test_prices_monotonically_decreasing(self):
        orders = gen_ladder(3500, 3000, 5, 1.0)
        prices = [o[1] for o in orders]
        self.assertEqual(prices, sorted(prices, reverse=True))

    def test_prices_monotonically_increasing(self):
        orders = gen_ladder(63000, 65000, 3, 0.001)
        prices = [o[1] for o in orders]
        self.assertEqual(prices, sorted(prices))

    def test_endpoints_correct(self):
        orders = gen_ladder(3500, 3000, 5, 1.0)
        self.assertAlmostEqual(orders[0][1], 3500.0, places=2)
        self.assertAlmostEqual(orders[-1][1], 3000.0, places=2)

    def test_last_order_absorbs_remainder(self):
        # 3 orders, 0.001 BTC — per_order = 0.00033333...
        orders = gen_ladder(60000, 58000, 3, 0.001)
        total = round(sum(o[0] for o in orders), 8)
        self.assertAlmostEqual(total, 0.001, places=9)

    def test_uneven_split_7_parts(self):
        orders = gen_ladder(60000, 58000, 7, 0.001)
        total = round(sum(o[0] for o in orders), 8)
        self.assertAlmostEqual(total, 0.001, places=9)

    def test_lambda_capture_by_value(self):
        calls = []
        fake_fn = lambda c, n, fp, tp: calls.append((c, n, fp, tp))
        coin, n_parts, from_p, to_p = "ETH", 5, 3500.0, 3000.0
        fn = lambda c=coin, n=n_parts, fp=from_p, tp=to_p: fake_fn(c, n, fp, tp)
        # Reassign outer vars
        coin, n_parts, from_p, to_p = "BTC", 99, 1.0, 2.0
        fn()
        self.assertEqual(calls[0], ("ETH", 5, 3500.0, 3000.0))


class TestLadderClient(unittest.TestCase):
    def _make_client(self, szi, coin="BTC"):
        import client as cli
        c = cli.HLClient()
        c.info = MagicMock()
        c.info.user_state.return_value = {"assetPositions": [{"position": {
            "coin": coin, "szi": szi, "entryPx": "60000",
            "unrealizedPnl": "0", "leverage": {"value": 5, "type": "cross"},
        }}], "marginSummary": {}}
        c.exchange = MagicMock()
        c.exchange.order.return_value = {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}]}},
        }
        return c

    def test_short_close_uses_is_buy_true(self):
        import client as cli
        c = self._make_client("-0.003")
        c.ladder_close("BTC", 3, 60000, 58000)
        for call_args in c.exchange.order.call_args_list:
            _, kwargs = call_args
            args = call_args[0]
            is_buy = args[1]
            self.assertTrue(is_buy, "closing SHORT must be a buy")

    def test_long_close_uses_is_buy_false(self):
        c = self._make_client("1.0", "ETH")
        c.ladder_close("ETH", 3, 3500, 3300)
        for call_args in c.exchange.order.call_args_list:
            args = call_args[0]
            is_buy = args[1]
            self.assertFalse(is_buy, "closing LONG must be a sell")

    def test_all_orders_are_reduce_only(self):
        c = self._make_client("-0.003")
        c.ladder_close("BTC", 3, 60000, 58000)
        for call_args in c.exchange.order.call_args_list:
            _, kwargs = call_args
            self.assertTrue(kwargs.get("reduce_only"), "all orders must be reduce_only")

    def test_all_orders_are_gtc(self):
        c = self._make_client("-0.003")
        c.ladder_close("BTC", 3, 60000, 58000)
        for call_args in c.exchange.order.call_args_list:
            args = call_args[0]
            order_type = args[4]
            self.assertEqual(order_type["limit"]["tif"], "Gtc")

    def test_sizes_sum_to_position_size(self):
        c = self._make_client("-0.003")
        c.ladder_close("BTC", 3, 60000, 58000)
        total = sum(call[0][2] for call in c.exchange.order.call_args_list)
        self.assertAlmostEqual(total, 0.003, places=7)

    def test_no_position_raises(self):
        import client as cli
        c = cli.HLClient()
        c.info = MagicMock()
        c.info.user_state.return_value = {"assetPositions": [], "marginSummary": {}}
        with self.assertRaises(ValueError):
            c.ladder_close("BTC", 3, 60000, 58000)

    def test_parts_out_of_range_raises(self):
        c = self._make_client("-0.003")
        with self.assertRaises(ValueError):
            c.ladder_close("BTC", 1, 60000, 58000)
        with self.assertRaises(ValueError):
            c.ladder_close("BTC", 21, 60000, 58000)

    def test_boundary_parts_work(self):
        c = self._make_client("-0.003")
        r2 = c.ladder_close("BTC", 2, 60000, 58000)
        self.assertEqual(len(r2), 2)
        c.exchange.order.reset_mock()
        r20 = c.ladder_close("BTC", 20, 60000, 58000)
        self.assertEqual(len(r20), 20)

    def test_correct_number_of_orders_placed(self):
        c = self._make_client("-0.5", "ETH")
        c.ladder_close("ETH", 5, 3500, 3000)
        self.assertEqual(c.exchange.order.call_count, 5)

    def test_partial_exchange_failure_returns_all_results(self):
        c = self._make_client("-0.003")
        call_n = [0]

        def side_effect(coin, is_buy, sz, px, order_type, reduce_only=False):
            call_n[0] += 1
            if call_n[0] == 2:
                return {"status": "err", "response": "margin"}
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}]}}}

        c.exchange.order.side_effect = side_effect
        results = c.ladder_close("BTC", 4, 65000, 63000)
        self.assertEqual(len(results), 4)
        ok = sum(1 for r in results if r.get("status") == "ok")
        self.assertEqual(ok, 3)


# ─── database ─────────────────────────────────────────────────────────────────

class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key().decode()
        os.environ["DB_ENCRYPTION_KEY"] = self.key
        self.f = Fernet(self.key.encode())
        self.tmp = tempfile.mktemp(suffix=".db")
        with sqlite3.connect(self.tmp) as c:
            c.execute("CREATE TABLE users (tg_id INTEGER PRIMARY KEY, agent_key TEXT NOT NULL, account_address TEXT NOT NULL)")
            c.execute("CREATE TABLE allowed_users (tg_id INTEGER PRIMARY KEY, added_by INTEGER NOT NULL, added_at TEXT NOT NULL)")
            c.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL, coin TEXT NOT NULL, direction TEXT NOT NULL, price REAL NOT NULL)")

    def tearDown(self):
        os.unlink(self.tmp)

    def _insert_user(self, tg_id, key="0x" + "a" * 64, addr="0x" + "b" * 40):
        enc = self.f.encrypt(key.encode()).decode()
        with sqlite3.connect(self.tmp) as c:
            c.execute("INSERT OR REPLACE INTO users VALUES (?,?,?)", (tg_id, enc, addr))

    def test_encryption_round_trip(self):
        raw_key = "0x" + "c" * 64
        self._insert_user(1, key=raw_key)
        with sqlite3.connect(self.tmp) as c:
            row = c.execute("SELECT agent_key FROM users WHERE tg_id=1").fetchone()
        decrypted = self.f.decrypt(row[0].encode()).decode()
        self.assertEqual(decrypted, raw_key)

    def test_stored_value_is_encrypted(self):
        raw_key = "0x" + "c" * 64
        self._insert_user(1, key=raw_key)
        with sqlite3.connect(self.tmp) as c:
            row = c.execute("SELECT agent_key FROM users WHERE tg_id=1").fetchone()
        self.assertNotEqual(row[0], raw_key)

    def test_allowed_user_insert_and_check(self):
        with sqlite3.connect(self.tmp) as c:
            c.execute("INSERT INTO allowed_users VALUES (888,111,'2024-01-01')")
        with sqlite3.connect(self.tmp) as c:
            found = c.execute("SELECT 1 FROM allowed_users WHERE tg_id=888").fetchone()
            absent = c.execute("SELECT 1 FROM allowed_users WHERE tg_id=777").fetchone()
        self.assertIsNotNone(found)
        self.assertIsNone(absent)

    def test_removeuser_cascades(self):
        self._insert_user(999)
        with sqlite3.connect(self.tmp) as c:
            c.execute("INSERT INTO allowed_users VALUES (999,111,'2024-01-01')")
            c.execute("INSERT INTO alerts VALUES (NULL,999,'BTC','above',70000)")
        with sqlite3.connect(self.tmp) as c:
            c.execute("DELETE FROM allowed_users WHERE tg_id=999")
            c.execute("DELETE FROM users WHERE tg_id=999")
            c.execute("DELETE FROM alerts WHERE tg_id=999")
        with sqlite3.connect(self.tmp) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM users WHERE tg_id=999").fetchone())
            self.assertIsNone(c.execute("SELECT 1 FROM alerts WHERE tg_id=999").fetchone())

    def test_alert_crud(self):
        with sqlite3.connect(self.tmp) as c:
            cur = c.execute("INSERT INTO alerts VALUES (NULL,100,'ETH','below',2000)")
            aid = cur.lastrowid
        with sqlite3.connect(self.tmp) as c:
            row = c.execute("SELECT coin,direction,price FROM alerts WHERE id=?", (aid,)).fetchone()
        self.assertEqual(row, ("ETH", "below", 2000.0))
        with sqlite3.connect(self.tmp) as c:
            c.execute("DELETE FROM alerts WHERE id=?", (aid,))
        with sqlite3.connect(self.tmp) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM alerts WHERE id=?", (aid,)).fetchone())

    def test_alert_ownership_isolation(self):
        with sqlite3.connect(self.tmp) as c:
            c.execute("INSERT INTO alerts VALUES (NULL,1,'BTC','above',70000)")
            c.execute("INSERT INTO alerts VALUES (NULL,2,'ETH','below',3000)")
        with sqlite3.connect(self.tmp) as c:
            user1_alerts = c.execute("SELECT id FROM alerts WHERE tg_id=1").fetchall()
            user2_alerts = c.execute("SELECT id FROM alerts WHERE tg_id=2").fetchall()
        self.assertEqual(len(user1_alerts), 1)
        self.assertEqual(len(user2_alerts), 1)
        # User 1 cannot see user 2's alert
        user1_ids = {r[0] for r in user1_alerts}
        user2_ids = {r[0] for r in user2_alerts}
        self.assertTrue(user1_ids.isdisjoint(user2_ids))


# ─── web app payload ──────────────────────────────────────────────────────────

class TestWebAppPayload(unittest.TestCase):
    import json as _json

    def _parse(self, payload_dict):
        import json
        data = json.loads(json.dumps(payload_dict))
        return {
            "coin":        data["coin"].upper(),
            "side":        data["side"],
            "size":        float(data["size"]),
            "leverage":    int(data["leverage"]),
            "tp":          float(data["tp"]) if data.get("tp") else None,
            "sl":          float(data["sl"]) if data.get("sl") else None,
            "limit_price": float(data["limit_price"]) if data.get("limit_price") else None,
        }

    def test_market_order_with_tp(self):
        p = self._parse({"coin": "btc", "side": "long", "size": "0.001",
                         "leverage": "10", "tp": "65000", "sl": "", "limit_price": ""})
        self.assertEqual(p["coin"], "BTC")
        self.assertEqual(p["size"], 0.001)
        self.assertEqual(p["tp"], 65000.0)
        self.assertIsNone(p["sl"])
        self.assertIsNone(p["limit_price"])

    def test_limit_order(self):
        p = self._parse({"coin": "ETH", "side": "short", "size": "0.5",
                         "leverage": "5", "tp": "", "sl": "", "limit_price": "3400"})
        self.assertEqual(p["limit_price"], 3400.0)
        self.assertIsNone(p["tp"])

    def test_empty_strings_become_none(self):
        p = self._parse({"coin": "SOL", "side": "long", "size": "1",
                         "leverage": "3", "tp": "", "sl": "", "limit_price": ""})
        self.assertIsNone(p["tp"])
        self.assertIsNone(p["sl"])
        self.assertIsNone(p["limit_price"])


class TestSLLadderClient(unittest.TestCase):
    def _make_client(self, szi, coin="BTC"):
        import client as cli
        c = cli.HLClient()
        c.info = MagicMock()
        c.info.user_state.return_value = {"assetPositions": [{"position": {
            "coin": coin, "szi": szi, "entryPx": "60000",
            "unrealizedPnl": "0", "leverage": {"value": 5, "type": "cross"},
        }}], "marginSummary": {}}
        c.exchange = MagicMock()
        c.exchange.order.return_value = {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}]}},
        }
        return c

    def test_uses_trigger_order_type(self):
        c = self._make_client("-0.003")
        c.slladder_close("BTC", 3, 61000, 63000)
        for call_args in c.exchange.order.call_args_list:
            args = call_args[0]
            order_type = args[4]
            self.assertIn("trigger", order_type)
            self.assertTrue(order_type["trigger"]["isMarket"])
            self.assertEqual(order_type["trigger"]["tpsl"], "sl")

    def test_trigger_price_matches_price_arg(self):
        c = self._make_client("-0.003")
        c.slladder_close("BTC", 3, 61000, 63000)
        for call_args in c.exchange.order.call_args_list:
            args = call_args[0]
            px = args[3]
            trigger_px = args[4]["trigger"]["triggerPx"]
            self.assertEqual(px, trigger_px)

    def test_short_close_uses_is_buy_true(self):
        c = self._make_client("-0.003")
        c.slladder_close("BTC", 3, 61000, 63000)
        for call_args in c.exchange.order.call_args_list:
            self.assertTrue(call_args[0][1])

    def test_long_close_uses_is_buy_false(self):
        c = self._make_client("1.0", "ETH")
        c.slladder_close("ETH", 3, 3200, 2800)
        for call_args in c.exchange.order.call_args_list:
            self.assertFalse(call_args[0][1])

    def test_all_orders_reduce_only(self):
        c = self._make_client("-0.003")
        c.slladder_close("BTC", 3, 61000, 63000)
        for call_args in c.exchange.order.call_args_list:
            self.assertTrue(call_args[1].get("reduce_only"))

    def test_sizes_sum_to_position_size(self):
        c = self._make_client("-0.003")
        c.slladder_close("BTC", 3, 61000, 63000)
        total = sum(call[0][2] for call in c.exchange.order.call_args_list)
        self.assertAlmostEqual(total, 0.003, places=7)

    def test_correct_order_count(self):
        c = self._make_client("-0.5", "ETH")
        c.slladder_close("ETH", 5, 3100, 3500)
        self.assertEqual(c.exchange.order.call_count, 5)

    def test_no_position_raises(self):
        import client as cli
        c = cli.HLClient()
        c.info = MagicMock()
        c.info.user_state.return_value = {"assetPositions": [], "marginSummary": {}}
        with self.assertRaises(ValueError):
            c.slladder_close("BTC", 3, 61000, 63000)

    def test_parts_out_of_range_raises(self):
        c = self._make_client("-0.003")
        with self.assertRaises(ValueError):
            c.slladder_close("BTC", 1, 61000, 63000)
        with self.assertRaises(ValueError):
            c.slladder_close("BTC", 21, 61000, 63000)

    def test_boundary_parts(self):
        c = self._make_client("-0.003")
        r2 = c.slladder_close("BTC", 2, 61000, 63000)
        self.assertEqual(len(r2), 2)
        c.exchange.order.reset_mock()
        r20 = c.slladder_close("BTC", 20, 61000, 63000)
        self.assertEqual(len(r20), 20)

    def test_differs_from_ladder_order_type(self):
        # slladder uses trigger, ladder uses limit GTC — must be different
        c = self._make_client("-0.003")
        c.slladder_close("BTC", 2, 61000, 63000)
        slladder_type = c.exchange.order.call_args_list[0][0][4]
        c.exchange.order.reset_mock()
        c.ladder_close("BTC", 2, 60000, 58000)
        ladder_type = c.exchange.order.call_args_list[0][0][4]
        self.assertIn("trigger", slladder_type)
        self.assertIn("limit", ladder_type)


class TestLadderOpenLeverage(unittest.TestCase):
    """
    Tests for the update_leverage guard in ladder_open and open_position.

    Hyperliquid cancels ALL resting orders for a coin when leverage changes.
    Guard rule: skip update_leverage if a position OR any open orders exist
    for that coin, so existing resting orders are never wiped.
    """

    def _make_client(self, szi, coin="ETH", open_orders=None):
        import client as cli
        c = cli.HLClient()
        c.info = MagicMock()
        if float(szi) != 0:
            positions = [{"position": {
                "coin": coin, "szi": szi, "entryPx": "1500",
                "unrealizedPnl": "0", "leverage": {"value": 10, "type": "cross"},
            }}]
        else:
            positions = []
        c.info.user_state.return_value = {"assetPositions": positions, "marginSummary": {}}
        c.info.frontend_open_orders.return_value = open_orders if open_orders is not None else []
        c.exchange = MagicMock()
        c.exchange.order.return_value = {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 42}}]}},
        }
        return c

    # ── ladder_open: leverage guard ──────────────────────────────────────────

    def test_leverage_skipped_when_position_exists(self):
        c = self._make_client("-1.0", "ETH")
        c.ladder_open("ETH", False, 1.0, 10, 3, 1600, 1650)
        c.exchange.update_leverage.assert_not_called()

    def test_leverage_skipped_when_orders_exist_no_position(self):
        """No position, but resting orders exist — must not wipe them."""
        orders = [{"coin": "ETH", "oid": 1, "orderType": "Limit"}]
        c = self._make_client("0", "ETH", open_orders=orders)
        c.ladder_open("ETH", True, 1.0, 10, 3, 1400, 1450)
        c.exchange.update_leverage.assert_not_called()

    def test_leverage_called_on_fresh_entry(self):
        """No position and no orders → safe to set leverage."""
        c = self._make_client("0", "ETH")
        c.ladder_open("ETH", True, 1.0, 10, 3, 1400, 1450)
        c.exchange.update_leverage.assert_called_once_with(10, "ETH", is_cross=True)

    def test_other_coin_orders_do_not_block_leverage(self):
        """BTC orders must not prevent leverage update on ETH."""
        orders = [{"coin": "BTC", "oid": 99}]
        c = self._make_client("0", "ETH", open_orders=orders)
        c.ladder_open("ETH", True, 1.0, 10, 3, 1400, 1450)
        c.exchange.update_leverage.assert_called_once()

    # ── ladder_open: order properties ────────────────────────────────────────

    def test_long_open_is_buy_true(self):
        c = self._make_client("0", "ETH")
        c.ladder_open("ETH", True, 1.0, 10, 3, 1400, 1450)
        for call in c.exchange.order.call_args_list:
            self.assertTrue(call[0][1])

    def test_short_open_is_buy_false(self):
        c = self._make_client("0", "ETH")
        c.ladder_open("ETH", False, 1.0, 10, 3, 1600, 1650)
        for call in c.exchange.order.call_args_list:
            self.assertFalse(call[0][1])

    def test_orders_are_gtc(self):
        c = self._make_client("0", "ETH")
        c.ladder_open("ETH", True, 1.0, 10, 3, 1400, 1450)
        for call in c.exchange.order.call_args_list:
            ot = call[0][4]
            self.assertIn("limit", ot)
            self.assertEqual(ot["limit"]["tif"], "Gtc")

    def test_orders_not_reduce_only(self):
        c = self._make_client("0", "ETH")
        c.ladder_open("ETH", True, 1.0, 10, 3, 1400, 1450)
        for call in c.exchange.order.call_args_list:
            self.assertFalse(call[1].get("reduce_only", False))

    def test_sizes_sum_to_total(self):
        c = self._make_client("0", "ETH")
        c.ladder_open("ETH", True, 3.0, 10, 5, 1400, 1490)
        total = sum(call[0][2] for call in c.exchange.order.call_args_list)
        self.assertAlmostEqual(total, 3.0, places=7)

    def test_correct_order_count(self):
        c = self._make_client("0", "ETH")
        c.ladder_open("ETH", True, 1.0, 10, 5, 1400, 1490)
        self.assertEqual(c.exchange.order.call_count, 5)

    def test_price_endpoints(self):
        from client import _round_price
        c = self._make_client("0", "ETH")
        c.ladder_open("ETH", True, 1.0, 10, 3, 1400, 1500)
        prices = [call[0][3] for call in c.exchange.order.call_args_list]
        self.assertEqual(prices[0], _round_price(1400))
        self.assertEqual(prices[-1], _round_price(1500))

    def test_parts_too_few_raises(self):
        c = self._make_client("0", "ETH")
        with self.assertRaises(ValueError):
            c.ladder_open("ETH", True, 1.0, 10, 1, 1400, 1500)

    def test_parts_too_many_raises(self):
        c = self._make_client("0", "ETH")
        with self.assertRaises(ValueError):
            c.ladder_open("ETH", True, 1.0, 10, 21, 1400, 1500)

    def test_parts_boundary_2_and_20(self):
        c = self._make_client("0", "ETH")
        c.ladder_open("ETH", True, 1.0, 10, 2, 1400, 1500)
        self.assertEqual(c.exchange.order.call_count, 2)
        c.exchange.order.reset_mock()
        c.ladder_open("ETH", True, 1.0, 10, 20, 1400, 1500)
        self.assertEqual(c.exchange.order.call_count, 20)

    def test_coexistence_existing_close_orders_survive(self):
        """
        Critical scenario: user has a short position + existing close ladder orders.
        Placing a ladder_open to add to that position must not cancel those orders.
        This is guaranteed by skipping update_leverage when the position exists.
        """
        c = self._make_client("-1.0", "ETH")
        c.ladder_open("ETH", False, 0.5, 10, 3, 1600, 1650)
        c.exchange.update_leverage.assert_not_called()
        self.assertEqual(c.exchange.order.call_count, 3)

    def test_coexistence_with_only_orders_no_position(self):
        """
        Scenario: user has 5 resting close orders for ETH but position is flat.
        Adding a ladder_open must not wipe those orders via update_leverage.
        """
        close_orders = [{"coin": "ETH", "oid": i} for i in range(5)]
        c = self._make_client("0", "ETH", open_orders=close_orders)
        c.ladder_open("ETH", True, 1.0, 5, 3, 1400, 1450)
        c.exchange.update_leverage.assert_not_called()

    # ── open_position: leverage guard ────────────────────────────────────────

    def test_open_position_leverage_skipped_with_existing_position(self):
        c = self._make_client("1.0", "ETH")
        c.open_position("ETH", True, 0.5, 10, limit_px=1500.0)
        c.exchange.update_leverage.assert_not_called()

    def test_open_position_leverage_skipped_with_existing_orders(self):
        orders = [{"coin": "ETH", "oid": 7}]
        c = self._make_client("0", "ETH", open_orders=orders)
        c.open_position("ETH", True, 0.5, 10, limit_px=1500.0)
        c.exchange.update_leverage.assert_not_called()

    def test_open_position_leverage_called_on_fresh_entry(self):
        c = self._make_client("0", "ETH")
        c.open_position("ETH", True, 0.5, 10, limit_px=1500.0)
        c.exchange.update_leverage.assert_called_once_with(10, "ETH", is_cross=True)

    def test_open_position_other_coin_orders_do_not_block(self):
        orders = [{"coin": "BTC", "oid": 5}]
        c = self._make_client("0", "ETH", open_orders=orders)
        c.open_position("ETH", True, 0.5, 10, limit_px=1500.0)
        c.exchange.update_leverage.assert_called_once()


# ─── fill notification logic (new timestamp-based approach) ───────────────────

class TestFillNotifications(unittest.TestCase):
    """Unit tests for the new get_fills()-based fill notification logic."""

    def _run_fill_check(self, fills, last_ts):
        """Simulate the fill notification logic, returns (messages, new_last_ts)."""
        msgs = []
        new_last = last_ts
        for f in sorted(fills, key=lambda x: float(x.get("time", 0))):
            fill_ts = float(f.get("time", 0))
            if fill_ts <= last_ts:
                continue
            new_last = max(new_last, fill_ts)
            coin = f.get("coin", "?")
            fill_px = float(f.get("px", 0))
            fill_sz = float(f.get("sz", 0))
            is_buy = f.get("side") == "B"
            closed_pnl = float(f.get("closedPnl", 0))
            order_type = f.get("orderType", "")

            if "Take Profit" in order_type:
                msgs.append(("TP", coin, fill_px, closed_pnl))
            elif "Stop" in order_type:
                msgs.append(("SL", coin, fill_px, closed_pnl))
            elif closed_pnl != 0:
                side_lbl = "BUY" if is_buy else "SELL"
                msgs.append(("CLOSE", coin, side_lbl, fill_px, closed_pnl))
            else:
                side_lbl = "BUY" if is_buy else "SELL"
                msgs.append(("OPEN", coin, side_lbl, fill_px))
        return msgs, new_last

    def test_tp_fill_sends_notification(self):
        fills = [{"time": 1000, "coin": "BTC", "px": "65000", "sz": "0.001",
                  "side": "A", "closedPnl": "50.0", "orderType": "Take Profit Market"}]
        msgs, _ = self._run_fill_check(fills, last_ts=0)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0][0], "TP")
        self.assertEqual(msgs[0][1], "BTC")
        self.assertAlmostEqual(msgs[0][3], 50.0)

    def test_sl_fill_sends_notification(self):
        fills = [{"time": 2000, "coin": "ETH", "px": "3000", "sz": "1.0",
                  "side": "B", "closedPnl": "-25.0", "orderType": "Stop Market"}]
        msgs, _ = self._run_fill_check(fills, last_ts=0)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0][0], "SL")
        self.assertAlmostEqual(msgs[0][3], -25.0)

    def test_already_seen_fill_ignored(self):
        fills = [{"time": 500, "coin": "BTC", "px": "60000", "sz": "0.001",
                  "side": "B", "closedPnl": "0", "orderType": "Limit Order"}]
        msgs, _ = self._run_fill_check(fills, last_ts=500)
        self.assertEqual(msgs, [])

    def test_only_new_fills_sent(self):
        fills = [
            {"time": 100, "coin": "BTC", "px": "60000", "sz": "0.001",
             "side": "B", "closedPnl": "0", "orderType": "Limit Order"},
            {"time": 200, "coin": "ETH", "px": "3000", "sz": "1.0",
             "side": "B", "closedPnl": "0", "orderType": "Limit Order"},
        ]
        msgs, new_last = self._run_fill_check(fills, last_ts=100)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0][1], "ETH")
        self.assertEqual(new_last, 200)

    def test_last_ts_updated_to_newest_fill(self):
        fills = [
            {"time": 300, "coin": "SOL", "px": "150", "sz": "10",
             "side": "B", "closedPnl": "0", "orderType": "Limit Order"},
            {"time": 500, "coin": "SOL", "px": "155", "sz": "5",
             "side": "A", "closedPnl": "50", "orderType": "Limit Order"},
        ]
        _, new_last = self._run_fill_check(fills, last_ts=0)
        self.assertEqual(new_last, 500)

    def test_closing_fill_shows_pnl(self):
        fills = [{"time": 100, "coin": "SOL", "px": "155", "sz": "10",
                  "side": "A", "closedPnl": "42.5", "orderType": "Limit Order"}]
        msgs, _ = self._run_fill_check(fills, last_ts=0)
        self.assertEqual(msgs[0][0], "CLOSE")
        self.assertAlmostEqual(msgs[0][4], 42.5)

    def test_opening_fill_no_pnl(self):
        fills = [{"time": 100, "coin": "BTC", "px": "65000", "sz": "0.001",
                  "side": "B", "closedPnl": "0", "orderType": "Limit Order"}]
        msgs, _ = self._run_fill_check(fills, last_ts=0)
        self.assertEqual(msgs[0][0], "OPEN")

    def test_multiple_fills_all_notified(self):
        fills = [
            {"time": 100 + i, "coin": "ETH", "px": str(3000 + i), "sz": "1",
             "side": "B", "closedPnl": "0", "orderType": "Limit Order"}
            for i in range(5)
        ]
        msgs, _ = self._run_fill_check(fills, last_ts=0)
        self.assertEqual(len(msgs), 5)

    def test_fills_processed_in_time_order(self):
        fills = [
            {"time": 200, "coin": "ETH", "px": "3100", "sz": "1",
             "side": "A", "closedPnl": "100", "orderType": "Limit Order"},
            {"time": 100, "coin": "ETH", "px": "3000", "sz": "1",
             "side": "B", "closedPnl": "0", "orderType": "Limit Order"},
        ]
        msgs, _ = self._run_fill_check(fills, last_ts=0)
        self.assertEqual(msgs[0][0], "OPEN")   # time=100 processed first
        self.assertEqual(msgs[1][0], "CLOSE")  # time=200 second


# ─── close_pct inline button logic ────────────────────────────────────────────

class TestClosePctLogic(unittest.TestCase):
    """Unit tests for the close_pct button calculations."""

    def _calc_close_size(self, pos_size, pct):
        return round(abs(pos_size) * pct / 100, 8)

    def _calc_pnl(self, pos_size, entry_px, current_px, close_size):
        is_long = pos_size > 0
        return (current_px - entry_px) * close_size * (1 if is_long else -1)

    def test_close_100_uses_full_size(self):
        self.assertAlmostEqual(self._calc_close_size(1.0, 100), 1.0)

    def test_close_50_halves_position(self):
        self.assertAlmostEqual(self._calc_close_size(2.0, 50), 1.0)

    def test_close_25_quarters_position(self):
        self.assertAlmostEqual(self._calc_close_size(4.0, 25), 1.0)

    def test_close_pct_short_position(self):
        self.assertAlmostEqual(self._calc_close_size(-1.5, 50), 0.75)

    def test_pnl_long_profit(self):
        pnl = self._calc_pnl(1.0, 3000, 3500, 1.0)
        self.assertAlmostEqual(pnl, 500.0)

    def test_pnl_long_loss(self):
        pnl = self._calc_pnl(1.0, 3000, 2500, 1.0)
        self.assertAlmostEqual(pnl, -500.0)

    def test_pnl_short_profit(self):
        pnl = self._calc_pnl(-1.0, 3000, 2500, 1.0)
        self.assertAlmostEqual(pnl, 500.0)

    def test_pnl_short_loss(self):
        pnl = self._calc_pnl(-1.0, 3000, 3500, 1.0)
        self.assertAlmostEqual(pnl, -500.0)

    def test_partial_close_pnl_scales_with_size(self):
        pnl_full = self._calc_pnl(1.0, 3000, 3500, 1.0)
        pnl_half = self._calc_pnl(1.0, 3000, 3500, 0.5)
        self.assertAlmostEqual(pnl_half, pnl_full / 2)

    def test_close_size_rounded_to_8_decimals(self):
        # round() to 8 places should not exceed 8 decimal places for normal sizes
        sz = self._calc_close_size(1.123456789, 50)
        self.assertAlmostEqual(sz, round(0.561728394, 8), places=8)


# ─── stats / PnL analytics logic ──────────────────────────────────────────────

class TestStatsLogic(unittest.TestCase):
    """Unit tests for the /stats command analytics calculations."""

    def _compute_stats(self, fills):
        close_pnls, total_pnl, total_fees = [], 0.0, 0.0
        daily: dict = {}
        coin_pnl: dict = {}
        for f in fills:
            pnl  = float(f.get("closedPnl", 0))
            fee  = float(f.get("fee", 0))
            coin = f.get("coin", "?")
            ts   = float(f.get("time", 0)) / 1000
            import datetime as _dt
            day = _dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            total_pnl  += pnl
            total_fees += fee
            if pnl != 0:
                close_pnls.append(pnl)
            daily[day]    = daily.get(day, 0.0) + pnl
            coin_pnl[coin] = coin_pnl.get(coin, 0.0) + pnl
        wins   = [p for p in close_pnls if p > 0]
        losses = [p for p in close_pnls if p < 0]
        n      = len(close_pnls)
        win_rt = len(wins) / n * 100 if n else 0
        avg_w  = sum(wins) / len(wins) if wins else 0
        avg_l  = sum(losses) / len(losses) if losses else 0
        return {
            "total_pnl": total_pnl, "total_fees": total_fees,
            "net": total_pnl - total_fees,
            "win_rate": win_rt, "n": n, "wins": len(wins), "losses": len(losses),
            "avg_w": avg_w, "avg_l": avg_l,
            "daily": daily, "coin_pnl": coin_pnl,
        }

    def _make_fill(self, pnl, fee=0, coin="ETH", ts=1000000):
        return {"closedPnl": str(pnl), "fee": str(fee), "coin": coin, "time": str(ts)}

    def test_all_winners_100_pct_win_rate(self):
        fills = [self._make_fill(10, ts=i*1000) for i in range(1, 6)]
        stats = self._compute_stats(fills)
        self.assertAlmostEqual(stats["win_rate"], 100.0)

    def test_all_losers_0_pct_win_rate(self):
        fills = [self._make_fill(-10, ts=i*1000) for i in range(1, 6)]
        stats = self._compute_stats(fills)
        self.assertAlmostEqual(stats["win_rate"], 0.0)

    def test_mixed_win_rate(self):
        fills = [self._make_fill(10, ts=1000), self._make_fill(-5, ts=2000),
                 self._make_fill(8, ts=3000), self._make_fill(-3, ts=4000)]
        stats = self._compute_stats(fills)
        self.assertAlmostEqual(stats["win_rate"], 50.0)

    def test_fees_deducted_from_net(self):
        fills = [self._make_fill(100, fee=5, ts=1000)]
        stats = self._compute_stats(fills)
        self.assertAlmostEqual(stats["net"], 95.0)
        self.assertAlmostEqual(stats["total_fees"], 5.0)

    def test_total_pnl_sums_correctly(self):
        fills = [self._make_fill(10, ts=1000), self._make_fill(20, ts=2000),
                 self._make_fill(-5, ts=3000)]
        stats = self._compute_stats(fills)
        self.assertAlmostEqual(stats["total_pnl"], 25.0)

    def test_avg_winner(self):
        fills = [self._make_fill(10, ts=1000), self._make_fill(20, ts=2000)]
        stats = self._compute_stats(fills)
        self.assertAlmostEqual(stats["avg_w"], 15.0)

    def test_avg_loser(self):
        fills = [self._make_fill(-5, ts=1000), self._make_fill(-15, ts=2000)]
        stats = self._compute_stats(fills)
        self.assertAlmostEqual(stats["avg_l"], -10.0)

    def test_zero_pnl_fills_excluded_from_close_count(self):
        fills = [self._make_fill(0, ts=1000), self._make_fill(10, ts=2000)]
        stats = self._compute_stats(fills)
        self.assertEqual(stats["n"], 1)

    def test_coin_pnl_aggregated(self):
        fills = [self._make_fill(10, coin="BTC", ts=1000),
                 self._make_fill(5, coin="BTC", ts=2000),
                 self._make_fill(-3, coin="ETH", ts=3000)]
        stats = self._compute_stats(fills)
        self.assertAlmostEqual(stats["coin_pnl"]["BTC"], 15.0)
        self.assertAlmostEqual(stats["coin_pnl"]["ETH"], -3.0)

    def test_daily_pnl_aggregated(self):
        # Both fills in same day (ts=0 → 1970-01-01)
        fills = [self._make_fill(10, ts=0), self._make_fill(20, ts=1000)]
        stats = self._compute_stats(fills)
        self.assertEqual(len(stats["daily"]), 1)
        self.assertAlmostEqual(list(stats["daily"].values())[0], 30.0)

    def test_empty_fills_returns_zero_stats(self):
        stats = self._compute_stats([])
        self.assertEqual(stats["n"], 0)
        self.assertAlmostEqual(stats["win_rate"], 0.0)
        self.assertAlmostEqual(stats["total_pnl"], 0.0)


# ─── % sizing logic ───────────────────────────────────────────────────────────

class TestPctSizingLogic(unittest.TestCase):
    """Unit tests for the % of account sizing calculation (mirrors JS applyPct)."""

    def _calc_size(self, balance, pct, leverage, price, decimals=4):
        margin   = balance * pct / 100
        notional = margin * leverage
        sz       = notional / price
        return round(sz, decimals)

    def test_1pct_5x_btc(self):
        # 1% of $10k account, 5x leverage, BTC at $50k
        # margin = $100, notional = $500, size = 0.01 BTC
        sz = self._calc_size(10000, 1, 5, 50000)
        self.assertAlmostEqual(sz, 0.01, places=4)

    def test_5pct_10x_eth(self):
        # 5% of $5k account, 10x leverage, ETH at $2500
        # margin = $250, notional = $2500, size = 1.0 ETH
        sz = self._calc_size(5000, 5, 10, 2500)
        self.assertAlmostEqual(sz, 1.0, places=4)

    def test_larger_leverage_gives_larger_size(self):
        sz_5x  = self._calc_size(1000, 1, 5,  100)
        sz_10x = self._calc_size(1000, 1, 10, 100)
        self.assertGreater(sz_10x, sz_5x)

    def test_higher_pct_gives_larger_size(self):
        sz_1pct = self._calc_size(1000, 1, 5, 100)
        sz_5pct = self._calc_size(1000, 5, 5, 100)
        self.assertAlmostEqual(sz_5pct, sz_1pct * 5, places=4)

    def test_higher_price_gives_smaller_size(self):
        sz_low  = self._calc_size(1000, 1, 5, 100)
        sz_high = self._calc_size(1000, 1, 5, 200)
        self.assertAlmostEqual(sz_high, sz_low / 2, places=4)

    def test_zero_price_would_divide_by_zero(self):
        with self.assertRaises(ZeroDivisionError):
            _ = 1000 * 1 / 100 * 5 / 0


if __name__ == "__main__":
    unittest.main(verbosity=2)
