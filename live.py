"""LIVE grid trading -- this places REAL orders with REAL money on Pionex.

Do not run this until:
  1. You have applied for a Pionex API key yourself (see README.md) and put
     it in .env (PIONEX_API_KEY / PIONEX_API_SECRET).
  2. You have run simulate.py against the same symbol/range/grids and are
     satisfied with the result.
  3. You have set LIVE_TRADING_CONFIRMED=I_UNDERSTAND_THIS_RISKS_REAL_MONEY
     in .env.
  4. You pass --i-understand-real-money-is-at-risk on the command line.

Even with all of the above, this script asks for one more typed
confirmation before placing the first order, and re-prints the exact
config (symbol, range, grid count, capital, stop-loss) so you can abort.

Claude will not run this script for you with real credentials -- start it
yourself once you're ready, per the project's safety rules around actions
that move real money.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from grid_strategy import GridConfig
from pionex_client import PionexClient

REQUIRED_CONFIRMATION = "I_UNDERSTAND_THIS_RISKS_REAL_MONEY"
STATE_FILE = Path(__file__).parent / "live_state.json"


class LiveGridTrader:
    """Places REAL limit orders at each grid line and reacts to fills.

    Unlike GridEngine (which simulates instant fills as price crosses a
    line), this seeds real resting LIMIT orders below the current price and
    waits for Pionex's matching engine to fill them -- same mechanism
    Pionex's own grid bot uses. When a BUY fills, a SELL is placed one line
    up; when that SELL fills, a fresh BUY is placed back at the original
    line, keeping the grid re-seeded.

    NOTE: the exact shape of place_order()'s response (assumed here to be
    resp["data"]["orderId"]) is inferred from the official docs' prose
    ("Returns orderId and clientOrderId") but not from a worked example --
    verify this against your first real order's response and adjust if the
    envelope differs.
    """

    def __init__(self, client: PionexClient, config: GridConfig):
        self.client = client
        self.config = config
        self.lines = config.grid_lines
        self.orders: dict[int, dict] = self._load_state()
        self.starting_equity: float | None = None

    def _load_state(self) -> dict[int, dict]:
        if STATE_FILE.exists():
            return {int(k): v for k, v in json.loads(STATE_FILE.read_text()).items()}
        return {}

    def _save_state(self) -> None:
        STATE_FILE.write_text(json.dumps(self.orders))

    def _equity(self) -> tuple[float, float]:
        base, quote = self.config.symbol.split("_")
        balances = {b.coin: b for b in self.client.get_balances()}
        price = float(self.client.get_ticker(self.config.symbol)[0]["close"])
        base_amt = (balances[base].free + balances[base].frozen) if base in balances else 0.0
        quote_amt = (balances[quote].free + balances[quote].frozen) if quote in balances else 0.0
        return quote_amt + base_amt * price, price

    def bootstrap(self) -> None:
        """Seed initial BUY orders at every grid line below current price,
        up to the max-concurrent-capital budget. Skips lines that already
        have a tracked open order (e.g. resuming after a restart)."""
        equity, price = self._equity()
        self.starting_equity = equity
        deployed = sum(o["qty"] * o["line"] for o in self.orders.values())

        for idx in range(len(self.lines) - 1):
            if idx in self.orders:
                continue
            buy_line = self.lines[idx]
            if buy_line >= price:
                continue
            if deployed + self.config.capital_per_grid > self.config.max_concurrent_capital:
                break
            qty = round(self.config.capital_per_grid / buy_line, 6)
            resp = self.client.place_order(
                self.config.symbol, "BUY", "LIMIT", size=str(qty), price=str(buy_line),
                client_order_id=f"grid-{idx}-{int(time.time())}",
            )
            order_id = resp["data"]["orderId"]
            self.orders[idx] = {"order_id": order_id, "side": "BUY", "line": buy_line, "qty": qty}
            deployed += qty * buy_line
        self._save_state()

    def poll_once(self) -> str:
        equity, price = self._equity()
        drawdown = (self.starting_equity - equity) / self.starting_equity
        if drawdown >= self.config.portfolio_stop_loss_pct:
            self._flatten()
            return f"stopped: portfolio stop-loss hit ({drawdown:.1%} drawdown)"

        open_order_ids = {o["orderId"] for o in self.client.get_open_orders(self.config.symbol)}
        for idx, info in list(self.orders.items()):
            if info["order_id"] not in open_order_ids:
                self._on_filled(idx, info)
        self._save_state()
        return f"ok: price={price} equity={equity:.2f} open_lines={len(self.orders)}"

    def _on_filled(self, idx: int, info: dict) -> None:
        if info["side"] == "BUY":
            sell_line = self.lines[idx + 1]
            resp = self.client.place_order(
                self.config.symbol, "SELL", "LIMIT", size=str(info["qty"]), price=str(sell_line),
                client_order_id=f"grid-{idx}-sell-{int(time.time())}",
            )
            self.orders[idx] = {"order_id": resp["data"]["orderId"], "side": "SELL",
                                 "line": sell_line, "qty": info["qty"]}
        else:
            buy_line = self.lines[idx]
            qty = round(self.config.capital_per_grid / buy_line, 6)
            resp = self.client.place_order(
                self.config.symbol, "BUY", "LIMIT", size=str(qty), price=str(buy_line),
                client_order_id=f"grid-{idx}-buy-{int(time.time())}",
            )
            self.orders[idx] = {"order_id": resp["data"]["orderId"], "side": "BUY",
                                 "line": buy_line, "qty": qty}

    def _flatten(self) -> None:
        self.client.cancel_all_orders(self.config.symbol)
        base, _ = self.config.symbol.split("_")
        balances = {b.coin: b for b in self.client.get_balances()}
        if base in balances and balances[base].free > 0:
            self.client.place_order(self.config.symbol, "SELL", "MARKET",
                                     size=str(balances[base].free))
        self.orders.clear()
        self._save_state()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC_USDT")
    parser.add_argument("--lower", type=float, required=True)
    parser.add_argument("--upper", type=float, required=True)
    parser.add_argument("--grids", type=int, default=8)
    parser.add_argument("--capital", type=float, required=True)
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--i-understand-real-money-is-at-risk", action="store_true",
                         dest="confirmed_flag")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.getenv("PIONEX_API_KEY")
    api_secret = os.getenv("PIONEX_API_SECRET")
    env_confirmation = os.getenv("LIVE_TRADING_CONFIRMED", "")

    if not api_key or not api_secret:
        sys.exit("Missing PIONEX_API_KEY / PIONEX_API_SECRET in .env. See README.md.")
    if env_confirmation != REQUIRED_CONFIRMATION:
        sys.exit(
            f"Set LIVE_TRADING_CONFIRMED={REQUIRED_CONFIRMATION} in .env to enable live.py."
        )
    if not args.confirmed_flag:
        sys.exit("Pass --i-understand-real-money-is-at-risk to run live.py.")

    config = GridConfig(symbol=args.symbol, lower_price=args.lower, upper_price=args.upper,
                         grid_count=args.grids, total_capital=args.capital)

    print("=== LIVE TRADING -- REAL MONEY ===")
    print(f"symbol={config.symbol} range=[{config.lower_price}, {config.upper_price}] "
          f"grids={config.grid_count} capital={config.total_capital}")
    print(f"capital_per_grid={config.capital_per_grid:.2f} "
          f"max_concurrent_capital={config.max_concurrent_capital:.2f} "
          f"stop_loss={config.portfolio_stop_loss_pct:.0%}")
    typed = input('\nType exactly "PLACE REAL ORDERS" to continue: ')
    if typed != "PLACE REAL ORDERS":
        sys.exit("Confirmation text did not match. Aborting, nothing was sent to Pionex.")

    client = PionexClient(api_key=api_key, api_secret=api_secret)

    # Sanity check the credentials against a harmless authenticated call
    # before going any further.
    balances = client.get_balances()
    print("\nAccount balances OK:", [b for b in balances if b.free > 0 or b.frozen > 0])

    trader = LiveGridTrader(client, config)
    if STATE_FILE.exists():
        print(f"Resuming from existing {STATE_FILE.name} "
              f"({len(trader.orders)} tracked open order(s)).")
    trader.bootstrap()

    print(f"\nSeeded {len(trader.orders)} grid order(s). Polling every "
          f"{args.poll_seconds}s. Ctrl+C to stop the loop (does NOT cancel "
          f"resting orders -- rerun this script to resume, or cancel manually "
          f"on Pionex / via client.cancel_all_orders()).")
    try:
        while True:
            status = trader.poll_once()
            print(status)
            if status.startswith("stopped"):
                break
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        print("\nInterrupted. State saved to", STATE_FILE.name)


if __name__ == "__main__":
    main()
