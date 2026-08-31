"""LIVE technical-indicator trading -- SPOT ONLY, this places REAL orders
with REAL money on Pionex. No futures/leverage.

Same three safety gates as live.py:
  1. .env: LIVE_TRADING_CONFIRMED=I_UNDERSTAND_THIS_RISKS_REAL_MONEY
  2. CLI flag: --i-understand-real-money-is-at-risk
  3. Typed confirmation ("PLACE REAL ORDERS") before the first order

Claude will not run this script for you with real credentials -- start it
yourself once you're ready, per the project's safety rules around actions
that move real money.

Three interchangeable strategies (see ta_strategy.py for backtests):
  --strategy tiered-ma (DEFAULT): position-sizes in tiers off 3 SMAs
    (50/100/200-day) -- 100% invested above all three, 50% above just the
    long one, 0% below it. Best backtest so far: +1818% vs buy-and-hold's
    +1229% over ~9 years, 50.7% max drawdown vs buy-and-hold's 83.5%,
    robust across nearby MA-period choices. Still only tested on ONE
    historical BTC path -- see README's caveat before trusting this blindly.
  --strategy ma-filter: simpler, single SMA(200), all-in/all-out. +1003%,
    62.7% MDD. A reasonable fallback if you want fewer moving parts.
  --strategy ema-atr: oldest version, EMA(20/50) golden-cross entry with a
    2x-ATR stop / 3x-ATR take-profit. Backtested -4.4% -- kept only for
    comparison, not recommended. Uniquely among the three, this one checks
    stop-loss/take-profit against the LIVE price every poll (not just once
    per daily close), since it's the only one with an intra-trade stop to
    watch; the other two only re-evaluate once per new daily candle.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

from ta_strategy import ema_series, atr_series
from pionex_client import PionexClient

REQUIRED_CONFIRMATION = "I_UNDERSTAND_THIS_RISKS_REAL_MONEY"
STATE_FILE = Path(__file__).parent / "live_ta_state.json"
MA_STATE_FILE = Path(__file__).parent / "live_ma_filter_state.json"
TIERED_STATE_FILE = Path(__file__).parent / "live_tiered_ma_state.json"
PEAK_STATE_FILE = Path(__file__).parent / "live_peak_equity_state.json"
LOCK_FILE = Path(__file__).parent / "live_ta.lock"

# Below this notional value (in quote currency, e.g. USDT) we treat a
# balance as "dust" rather than a real position -- avoids endless tiny
# rebalance attempts caused by fee-driven precision loss, and avoids ever
# comparing a float balance to exactly 0.0.
DUST_THRESHOLD_QUOTE = 1.0


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write-to-temp-then-rename so a crash or power loss mid-write can
    never leave a half-written/corrupted state file behind (os.replace is
    atomic on both POSIX and Windows)."""
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _load_json_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: {path.name} is corrupted ({e}) -- starting from empty state. "
              f"If you had an open position, verify it manually on Pionex.")
        return {}


def _pid_is_running(pid: int) -> bool:
    """Liveness check only -- must never be able to terminate the target
    process. os.kill(pid, 0) is safe for this on POSIX (signal 0 is a pure
    query), but on Windows os.kill() maps non-special signals onto
    TerminateProcess -- calling it with pid 0 there risks killing whatever
    process now holds that PID. Use OpenProcess with a query-only access
    right on Windows instead."""
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock() -> None:
    """Refuse to start a second instance against the same state directory.
    Running two copies of this script at once would have both act on the
    same account balance independently -- e.g. both deciding to buy the
    same signal, doubling the position. Not airtight against every race
    (no OS-level file lock), but catches the common case: accidentally
    leaving one terminal running and starting another."""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
        except ValueError:
            pid = None
        if pid is not None and _pid_is_running(pid):
            sys.exit(f"Another instance appears to be running (PID {pid}, {LOCK_FILE.name} "
                     f"exists). If that's wrong (e.g. it crashed without cleaning up), "
                     f"delete {LOCK_FILE.name} and try again.")
        # stale lock file (process not running, or PID unreadable) -- reclaim it
    LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(lambda: LOCK_FILE.unlink(missing_ok=True))


def check_portfolio_stop_loss(client: PionexClient, symbol: str, max_drawdown_pct: float) -> str | None:
    """Independent safety net on top of whichever strategy is running: track
    the highest equity ever seen (persisted across restarts) and if current
    equity has fallen more than max_drawdown_pct below that peak, sell
    everything and return a message telling main() to halt the loop for
    good. Returns None if no action was needed.

    This exists because none of the three TA strategies has its own
    portfolio-level circuit breaker the way the grid engine does (18%
    auto-halt) -- tiered-ma and ma-filter rely entirely on their trend
    signal to de-risk, which backtested a max drawdown of 50.7% / 62.7%
    before reacting. This is a blunt backstop against something going
    wrong that the trend signal doesn't (or can't) catch in time -- not a
    replacement for understanding that the strategy itself can still ride
    a real drawdown most of the way down before this triggers.
    """
    base, quote = symbol.split("_")
    balances = {b.coin: b for b in client.get_balances()}
    price = float(client.get_ticker(symbol)[0]["close"])
    base_free = balances[base].free if base in balances else 0.0
    quote_free = balances[quote].free if quote in balances else 0.0
    equity = quote_free + base_free * price

    state = _load_json_state(PEAK_STATE_FILE)
    peak = max(state.get("peak_equity", equity), equity)

    drawdown = (peak - equity) / peak if peak > 0 else 0.0
    if drawdown >= max_drawdown_pct:
        if base_free * price > DUST_THRESHOLD_QUOTE:
            client.place_order(symbol, "SELL", "MARKET", size=str(base_free),
                                client_order_id=f"stoploss-{uuid.uuid4().hex[:16]}")
        _atomic_write_json(PEAK_STATE_FILE, {"peak_equity": peak, "halted": True})
        # every strategy's own state must agree the position is now flat, or a
        # later restart will trust its stale "I'm still invested" belief and
        # never re-enter even though the account is actually sitting in cash
        for f in (STATE_FILE, MA_STATE_FILE, TIERED_STATE_FILE):
            if f.exists():
                s = _load_json_state(f)
                s["position_qty"] = 0.0
                s["current_tier"] = 0.0
                s["entry_price"] = s["stop_price"] = s["take_profit_price"] = None
                _atomic_write_json(f, s)
        return (f"PORTFOLIO STOP-LOSS HIT: equity {equity:.2f} is {drawdown:.1%} below "
                f"peak {peak:.2f} (limit {max_drawdown_pct:.0%}). Sold all {base}. Halting.")

    _atomic_write_json(PEAK_STATE_FILE, {"peak_equity": peak, "halted": False})
    return None


class LiveTieredMATrader:
    """Position-sizes in tiers off three SMAs instead of one binary switch:
    100% invested above all three (short/mid/long), 50% above just the
    long SMA, 0% below it. See ta_strategy.run_tiered_ma_strategy for the
    logic and README.md for the backtest (currently the best result in
    the project: beat buy-and-hold's return AND cut its max drawdown,
    robust across nearby MA-period choices -- but still only tested on
    one historical BTC path, see the caveat in README).

    Only evaluated once per new daily candle. Rebalances via a single
    MARKET order sized to the notional delta between current and target
    tier value -- no continuous price-based stop to watch between polls."""

    def __init__(self, client: PionexClient, symbol: str, ma_short: int, ma_mid: int,
                 ma_long: int):
        self.client = client
        self.symbol = symbol
        self.base, self.quote = symbol.split("_")
        self.ma_short = ma_short
        self.ma_mid = ma_mid
        self.ma_long = ma_long
        self._load_state()

    def _load_state(self) -> None:
        state = _load_json_state(TIERED_STATE_FILE)
        self.last_bar_time = state.get("last_bar_time")

    def _save_state(self, current_tier: float) -> None:
        # current_tier is persisted for human-readable status only -- the
        # next check_daily_signal() call always re-derives the real tier
        # from actual account balance, never trusts this value, so a stale
        # or externally-changed balance (e.g. the portfolio stop-loss sold
        # everything) can never desync the trading decision.
        _atomic_write_json(TIERED_STATE_FILE, {
            "current_tier": current_tier, "last_bar_time": self.last_bar_time,
        })

    @property
    def position_qty(self) -> float:
        balances = {b.coin: b for b in self.client.get_balances()}
        return balances[self.base].free if self.base in balances else 0.0

    def _actual_tier(self, base_free: float, quote_free: float, price: float) -> float:
        equity = quote_free + base_free * price
        if equity <= 0:
            return 0.0
        invested_frac = (base_free * price) / equity
        return min((0.0, 0.5, 1.0), key=lambda t: abs(t - invested_frac))

    def check_daily_signal(self) -> str:
        from grid_strategy import sma
        limit = self.ma_long + 5
        klines = self.client.get_klines(self.symbol, interval="1D", limit=limit)
        klines = sorted(klines, key=lambda k: int(k["time"]))
        latest_time = int(klines[-1]["time"])
        if latest_time == self.last_bar_time:
            return "no new daily candle yet"

        closes = [float(k["close"]) for k in klines]
        i = len(klines) - 1
        close = closes[i]
        s_short = sma(closes, i, self.ma_short)
        s_mid = sma(closes, i, self.ma_mid)
        s_long = sma(closes, i, self.ma_long)
        if s_long is None:
            # not a trading decision, safe to mark this bar as seen
            self.last_bar_time = latest_time
            self._save_state(0.0)
            return "not enough history for SMA yet"

        if s_short is not None and s_mid is not None and close > s_short and close > s_mid and close > s_long:
            target_tier = 1.0
        elif close > s_long:
            target_tier = 0.5
        else:
            target_tier = 0.0

        balances = {b.coin: b for b in self.client.get_balances()}
        base_free = balances[self.base].free if self.base in balances else 0.0
        quote_free = balances[self.quote].free if self.quote in balances else 0.0
        actual_tier = self._actual_tier(base_free, quote_free, close)

        if target_tier == actual_tier:
            # only NOW is it safe to mark this bar as handled -- nothing
            # below can raise and leave last_bar_time silently out of sync
            self.last_bar_time = latest_time
            self._save_state(actual_tier)
            return f"holding tier {actual_tier:.0%}, no change (close={close})"

        equity_now = quote_free + base_free * close
        target_value = equity_now * target_tier
        current_value = base_free * close
        diff_value = target_value - current_value

        msg = f"tier {actual_tier:.0%} -> {target_tier:.0%} (close={close})"
        if diff_value > 0 and diff_value > DUST_THRESHOLD_QUOTE:
            spend = min(diff_value, quote_free)
            self.client.place_order(self.symbol, "BUY", "MARKET", amount=str(spend),
                                     client_order_id=f"tma-buy-{uuid.uuid4().hex[:16]}")
            msg += f", BUY ~{spend:.2f} {self.quote}"
        elif diff_value < 0 and -diff_value > DUST_THRESHOLD_QUOTE:
            sell_value = min(-diff_value, current_value)
            sell_qty = (sell_value / close) if close > 0 else 0
            sell_qty = min(sell_qty, base_free)
            if sell_qty > 0:
                self.client.place_order(self.symbol, "SELL", "MARKET", size=str(sell_qty),
                                         client_order_id=f"tma-sell-{uuid.uuid4().hex[:16]}")
                msg += f", SELL ~{sell_qty:.6f} {self.base}"
        else:
            msg += " (diff below dust threshold, skipped)"

        # only commit last_bar_time now that any order call above has
        # returned successfully -- if place_order raised, we never reach
        # here, so the next poll retries this same bar's signal instead of
        # silently skipping it
        self.last_bar_time = latest_time
        self._save_state(target_tier)
        return msg


class LiveMAFilterTrader:
    """All-in above SMA(ma_period), all-out below it. No ATR stop at all --
    see ta_strategy.run_ma_filter_strategy for why this simpler rule
    backtested far better (+1003% vs -4.4% over ~9 years) than the EMA+ATR
    version: a tight stop was whipsawing out of real uptrends on BTC's
    volatility. Only evaluated once per new daily candle -- no intraday
    price checks needed since there's no ATR stop/take-profit to watch."""

    def __init__(self, client: PionexClient, symbol: str, ma_period: int, position_pct: float):
        self.client = client
        self.symbol = symbol
        self.base, self.quote = symbol.split("_")
        self.ma_period = ma_period
        self.position_pct = position_pct
        self._load_state()

    def _load_state(self) -> None:
        state = _load_json_state(MA_STATE_FILE)
        self.last_bar_time = state.get("last_bar_time")

    @property
    def position_qty(self) -> float:
        balances = {b.coin: b for b in self.client.get_balances()}
        return balances[self.base].free if self.base in balances else 0.0

    def _save_state(self, position_qty: float) -> None:
        # position_qty is persisted for human-readable status only -- the
        # actual account balance is always re-checked fresh below, so this
        # value is never trusted for the trading decision itself.
        _atomic_write_json(MA_STATE_FILE, {
            "position_qty": position_qty, "last_bar_time": self.last_bar_time,
        })

    def check_daily_signal(self) -> str:
        from grid_strategy import sma
        limit = self.ma_period + 5
        klines = self.client.get_klines(self.symbol, interval="1D", limit=limit)
        klines = sorted(klines, key=lambda k: int(k["time"]))
        latest_time = int(klines[-1]["time"])
        if latest_time == self.last_bar_time:
            return "no new daily candle yet"

        closes = [float(k["close"]) for k in klines]
        i = len(klines) - 1
        trend = sma(closes, i, self.ma_period)
        close = closes[i]
        if trend is None:
            self.last_bar_time = latest_time
            self._save_state(0.0)
            return "not enough history for SMA yet"

        balances = {b.coin: b for b in self.client.get_balances()}
        base_free = balances[self.base].free if self.base in balances else 0.0
        quote_free = balances[self.quote].free if self.quote in balances else 0.0
        holding = (base_free * close) > DUST_THRESHOLD_QUOTE

        if not holding and close > trend:
            spend = quote_free * self.position_pct
            if spend > DUST_THRESHOLD_QUOTE:
                self.client.place_order(self.symbol, "BUY", "MARKET", amount=str(spend),
                                         client_order_id=f"maf-buy-{uuid.uuid4().hex[:16]}")
                self.last_bar_time = latest_time
                self._save_state(spend / close)
                return f"BUY: close {close} > SMA({self.ma_period}) {trend:.2f}, spent ~{spend:.2f} {self.quote}"
        elif holding and close < trend:
            self.client.place_order(self.symbol, "SELL", "MARKET", size=str(base_free),
                                     client_order_id=f"maf-sell-{uuid.uuid4().hex[:16]}")
            self.last_bar_time = latest_time
            self._save_state(0.0)
            return f"SELL: close {close} < SMA({self.ma_period}) {trend:.2f}"

        self.last_bar_time = latest_time
        self._save_state(base_free if holding else 0.0)
        return f"holding, no change: close={close} SMA({self.ma_period})={trend:.2f} base_free={base_free}"


class LiveTATrader:
    def __init__(self, client: PionexClient, symbol: str, ema_fast: int, ema_slow: int,
                 atr_period: int, stop_mult: float, tp_mult: float, position_pct: float):
        self.client = client
        self.symbol = symbol
        self.base, self.quote = symbol.split("_")
        self.ema_fast_period = ema_fast
        self.ema_slow_period = ema_slow
        self.atr_period = atr_period
        self.stop_mult = stop_mult
        self.tp_mult = tp_mult
        self.position_pct = position_pct
        self._load_state()

    def _load_state(self) -> None:
        state = _load_json_state(STATE_FILE)
        self.entry_price = state.get("entry_price")
        self.stop_price = state.get("stop_price")
        self.take_profit_price = state.get("take_profit_price")
        self.last_bar_time = state.get("last_bar_time")

    def _save_state(self) -> None:
        _atomic_write_json(STATE_FILE, {
            "entry_price": self.entry_price, "stop_price": self.stop_price,
            "take_profit_price": self.take_profit_price, "last_bar_time": self.last_bar_time,
        })

    @property
    def position_qty(self) -> float:
        # source of truth is the real account balance, never a persisted
        # guess -- see the tiered-ma/ma-filter traders' docstrings for why
        balances = {b.coin: b for b in self.client.get_balances()}
        return balances[self.base].free if self.base in balances else 0.0

    def _is_holding(self, base_free: float, price: float) -> bool:
        return (base_free * price) > DUST_THRESHOLD_QUOTE

    def _sell_all(self, reason: str) -> None:
        balances = {b.coin: b for b in self.client.get_balances()}
        base_free = balances[self.base].free if self.base in balances else 0.0
        price = float(self.client.get_ticker(self.symbol)[0]["close"])
        if self._is_holding(base_free, price):
            self.client.place_order(self.symbol, "SELL", "MARKET", size=str(base_free),
                                     client_order_id=f"emaatr-sell-{uuid.uuid4().hex[:16]}")
            print(f"SOLD {base_free} {self.base} ({reason})")
        self.entry_price = self.stop_price = self.take_profit_price = None
        self._save_state()

    def check_daily_signal(self) -> None:
        """Entry (golden cross) / trend-reversal exit (death cross), only
        acted on once per new daily candle."""
        limit = max(self.ema_slow_period, self.atr_period) + 5
        klines = self.client.get_klines(self.symbol, interval="1D", limit=limit)
        klines = sorted(klines, key=lambda k: int(k["time"]))
        latest_time = int(klines[-1]["time"])
        if latest_time == self.last_bar_time:
            return  # no new daily candle since last check

        closes = [float(k["close"]) for k in klines]
        highs = [float(k["high"]) for k in klines]
        lows = [float(k["low"]) for k in klines]
        fast = ema_series(closes, self.ema_fast_period)
        slow = ema_series(closes, self.ema_slow_period)
        atr = atr_series(highs, lows, closes, self.atr_period)
        i = len(klines) - 1
        if fast[i] is None or slow[i] is None or fast[i - 1] is None or slow[i - 1] is None:
            self.last_bar_time = latest_time
            self._save_state()
            return

        golden = fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]
        death = fast[i] < slow[i] and fast[i - 1] >= slow[i - 1]
        close = closes[i]

        balances = {b.coin: b for b in self.client.get_balances()}
        base_free = balances[self.base].free if self.base in balances else 0.0
        quote_free = balances[self.quote].free if self.quote in balances else 0.0
        holding = self._is_holding(base_free, close)

        if holding and self.entry_price is None:
            # balance shows a position but we have no record of its entry --
            # e.g. state was reset by the portfolio stop-loss, or the
            # position was opened some other way. Adopt today's price as a
            # (conservative, not reconstructible any other way) entry point
            # rather than leaving stop_price/take_profit_price as None.
            print(f"WARNING: holding {base_free} {self.base} but no entry_price on record -- "
                  f"treating {close} as entry for stop/take-profit purposes.")
            self.entry_price = close
            self.stop_price = close - self.stop_mult * atr[i]
            self.take_profit_price = close + self.tp_mult * atr[i]

        if not holding and golden:
            spend = quote_free * self.position_pct
            if spend <= DUST_THRESHOLD_QUOTE:
                print("Golden cross signal, but no meaningful quote balance to spend.")
            else:
                self.client.place_order(self.symbol, "BUY", "MARKET", amount=str(spend),
                                         client_order_id=f"emaatr-buy-{uuid.uuid4().hex[:16]}")
                self.entry_price = close
                self.stop_price = close - self.stop_mult * atr[i]
                self.take_profit_price = close + self.tp_mult * atr[i]
                print(f"BUY golden cross: spent ~{spend:.2f} {self.quote} at ~{close}, "
                      f"stop={self.stop_price:.2f} tp={self.take_profit_price:.2f}")
        elif holding and death:
            self._sell_all("death cross (trend reversal)")

        # only commit last_bar_time once everything above has completed
        # without raising -- an exception is retried against this same bar
        # next poll instead of being silently skipped
        self.last_bar_time = latest_time
        self._save_state()

    def check_stop_take_profit(self) -> str:
        balances = {b.coin: b for b in self.client.get_balances()}
        base_free = balances[self.base].free if self.base in balances else 0.0
        price = float(self.client.get_ticker(self.symbol)[0]["close"])
        if not self._is_holding(base_free, price):
            return "flat"
        if self.stop_price is None or self.take_profit_price is None:
            return (f"holding {base_free} {self.base} but stop/take-profit not set yet -- "
                    f"will be adopted on the next daily signal check")
        if price <= self.stop_price:
            self._sell_all(f"stop-loss hit (price {price} <= {self.stop_price:.2f})")
            return "stopped out"
        if price >= self.take_profit_price:
            self._sell_all(f"take-profit hit (price {price} >= {self.take_profit_price:.2f})")
            return "took profit"
        return f"holding, price={price} stop={self.stop_price:.2f} tp={self.take_profit_price:.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strategy", default="tiered-ma",
                         choices=["tiered-ma", "ma-filter", "ema-atr"],
                         help="tiered-ma (recommended, best backtest so far: +1818%% vs buy-hold's "
                              "+1229%% over ~9yr, 50.7%% max drawdown, robust across nearby MA "
                              "periods) or ma-filter (simpler single-200-SMA version, +1003%%, "
                              "62.7%% MDD) or ema-atr (oldest version, -4.4%%, not recommended)")
    parser.add_argument("--symbol", default="BTC_USDT")
    parser.add_argument("--ma-short", type=int, default=50, help="tiered-ma only")
    parser.add_argument("--ma-mid", type=int, default=100, help="tiered-ma only")
    parser.add_argument("--ma-long", type=int, default=200, help="tiered-ma / ma-filter")
    parser.add_argument("--ma-period", type=int, default=200, help="ma-filter only (alias of --ma-long)")
    parser.add_argument("--ema-fast", type=int, default=20, help="ema-atr only")
    parser.add_argument("--ema-slow", type=int, default=50, help="ema-atr only")
    parser.add_argument("--atr-period", type=int, default=14, help="ema-atr only")
    parser.add_argument("--stop-mult", type=float, default=2.0, help="ema-atr only")
    parser.add_argument("--tp-mult", type=float, default=3.0, help="ema-atr only")
    parser.add_argument("--position-pct", type=float, default=0.95,
                         help="fraction of quote-currency balance to spend per entry")
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--max-drawdown-pct", type=float, default=0.25,
                         help="portfolio-level circuit breaker: sell everything and halt for "
                              "good if equity falls this fraction below its highest-ever value. "
                              "Independent of whatever the strategy's own signal says.")
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
        sys.exit(f"Set LIVE_TRADING_CONFIRMED={REQUIRED_CONFIRMATION} in .env to enable this script.")
    if not args.confirmed_flag:
        sys.exit("Pass --i-understand-real-money-is-at-risk to run this script.")

    acquire_lock()

    print(f"=== LIVE {args.strategy.upper()} -- REAL MONEY (spot only, no leverage) ===")
    if args.strategy == "tiered-ma":
        print(f"symbol={args.symbol}  SMA({args.ma_short}/{args.ma_mid}/{args.ma_long})  "
              f"tiers: 100% above all three, 50% above just SMA({args.ma_long}), 0% below")
    elif args.strategy == "ma-filter":
        print(f"symbol={args.symbol}  SMA({args.ma_long})  position_pct={args.position_pct:.0%}")
    else:
        print(f"symbol={args.symbol}  EMA({args.ema_fast}/{args.ema_slow})  "
              f"ATR({args.atr_period})  stop={args.stop_mult}x  tp={args.tp_mult}x  "
              f"position_pct={args.position_pct:.0%}")
    typed = input('\nType exactly "PLACE REAL ORDERS" to continue: ')
    if typed != "PLACE REAL ORDERS":
        sys.exit("Confirmation text did not match. Aborting, nothing was sent to Pionex.")

    client = PionexClient(api_key=api_key, api_secret=api_secret)
    balances = client.get_balances()
    print("\nAccount balances OK:", [b for b in balances if b.free > 0 or b.frozen > 0])

    if args.strategy == "tiered-ma":
        trader = LiveTieredMATrader(client, args.symbol, args.ma_short, args.ma_mid, args.ma_long)
        state_file = TIERED_STATE_FILE
    elif args.strategy == "ma-filter":
        trader = LiveMAFilterTrader(client, args.symbol, args.ma_long, args.position_pct)
        state_file = MA_STATE_FILE
    else:
        trader = LiveTATrader(client, args.symbol, args.ema_fast, args.ema_slow,
                               args.atr_period, args.stop_mult, args.tp_mult, args.position_pct)
        state_file = STATE_FILE
    if state_file.exists():
        print(f"Resuming from {state_file.name}: position_qty={trader.position_qty}")

    print(f"\nPolling every {args.poll_seconds}s. Portfolio stop-loss: sells everything and "
          f"halts for good if equity drops {args.max_drawdown_pct:.0%} below its peak. "
          f"Ctrl+C to stop the loop without closing any position "
          f"(rerun to resume, or close manually on Pionex).\n"
          f"A network/API error will be logged and retried, not crash the script silently.")
    consecutive_errors = 0
    try:
        while True:
            try:
                stop_msg = check_portfolio_stop_loss(client, args.symbol, args.max_drawdown_pct)
                if stop_msg:
                    print(stop_msg)
                    break

                status = trader.check_daily_signal()
                if status:
                    print(status)
                if args.strategy == "ema-atr":
                    print(trader.check_stop_take_profit())
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                backoff = min(args.poll_seconds * consecutive_errors, 900.0)
                print(f"ERROR (attempt {consecutive_errors}): {type(e).__name__}: {e}. "
                      f"Retrying in {backoff:.0f}s. State on disk is unchanged by a failed attempt.")
                time.sleep(backoff)
                continue
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        print("\nInterrupted. State saved to", state_file.name)


if __name__ == "__main__":
    main()
