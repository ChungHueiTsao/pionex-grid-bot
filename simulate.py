"""Dry-run grid simulation against REAL Pionex public market data.

No API key needed, no orders are ever placed -- this only calls the public
GET /api/v1/market/klines endpoint and replays the grid logic in-memory.

Defaults to --strategy walk-forward (grid_strategy.run_walk_forward): the
grid's price range is recomputed weekly from ONLY the trailing 30 days of
data (never future data), so it's honest to backtest and safe to actually
deploy without hand-picking a range yourself. Backtested 2026-08-31 over
the full ~500-day BTC/USDT history as a single continuous run: -7.0% vs
-8.93% for simple buy-and-hold over the same window -- i.e. it lost less
than holding during a rough stretch for BTC, not that it was profitable in
absolute terms. See README.md "策略研究" for the full writeup, including an
earlier +1.56% result that turned out to have a look-ahead bias (the price
range had been hand-picked after seeing what price actually did) -- don't
trust that number.

--strategy trend-filtered and --strategy baseline require you to pass
--lower/--upper yourself (useful for testing a specific range you're
considering), and are more exposed to hindsight bias if you pick that
range by looking at a chart of what already happened.

Usage:
    python simulate.py --symbol BTC_USDT --capital 1000 --interval 1D --limit 200
    python simulate.py --strategy trend-filtered --lower 95000 --upper 130000 --grids 8 --capital 1000
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from grid_strategy import GridConfig, GridEngine, run_trend_filtered, run_walk_forward
from pionex_client import PionexClient


def run(symbol: str, grids: int, capital: float, interval: str, limit: int,
        end_date: str | None = None, strategy: str = "walk-forward",
        lower: float | None = None, upper: float | None = None,
        sma_period: int = 30, deviation_stop_pct: float = 0.05, spacing: str = "geometric",
        lookback_days: int = 30, buffer_low: float = 0.95, buffer_high: float = 1.10,
        recalibrate_every: int = 7) -> None:
    end_time = None
    if end_date:
        end_time = int(datetime.strptime(end_date, "%Y-%m-%d")
                        .replace(tzinfo=timezone.utc).timestamp() * 1000)

    client = PionexClient()  # public data only, no credentials

    # extra leading history to warm up whichever indicators the strategy needs
    warmup = {"walk-forward": max(sma_period, lookback_days),
              "trend-filtered": sma_period, "baseline": 0}[strategy]
    fetch_limit = min(limit + warmup, 500)
    klines = client.get_klines(symbol, interval=interval, limit=fetch_limit, end_time=end_time)
    if not klines:
        print("No kline data returned -- check the symbol / interval.")
        return

    klines = sorted(klines, key=lambda k: int(k["time"]))

    print(f"Symbol={symbol}  bars={len(klines)} ({interval})  strategy={strategy}  "
          f"spacing={spacing}")

    if strategy == "walk-forward":
        print(f"Range: recalibrated every {recalibrate_every}d from trailing "
              f"{lookback_days}d [low*{buffer_low}, high*{buffer_high}]  "
              f"Trend filter: SMA({sma_period})\n")
        engine, last_price, _equity_curve = run_walk_forward(
            klines, grids, capital, lookback_days, buffer_low, buffer_high,
            recalibrate_every, sma_period, deviation_stop_pct,
        )
    elif strategy == "trend-filtered":
        if lower is None or upper is None:
            raise ValueError("--lower/--upper are required for --strategy trend-filtered")
        config = GridConfig(symbol=symbol, lower_price=lower, upper_price=upper,
                             grid_count=grids, total_capital=capital, spacing=spacing)
        print(f"range=[{lower}, {upper}]  grids={grids}  capital={capital}")
        print(f"Trend filter: SMA({sma_period}), defensive exit at "
              f"{deviation_stop_pct:.0%} below SMA\n")
        engine, last_price = run_trend_filtered(klines, config, sma_period, deviation_stop_pct)
    elif strategy == "baseline":
        if lower is None or upper is None:
            raise ValueError("--lower/--upper are required for --strategy baseline")
        config = GridConfig(symbol=symbol, lower_price=lower, upper_price=upper,
                             grid_count=grids, total_capital=capital, spacing=spacing)
        print(f"range=[{lower}, {upper}]  grids={grids}  capital={capital}\n")
        engine = GridEngine(config)
        last_price = float(klines[0]["close"])
        for k in klines:
            ts = int(k["time"])
            for price in (float(k["open"]), float(k["low"]), float(k["high"]), float(k["close"])):
                engine.on_price(ts, price)
                last_price = price
            if engine.stopped:
                break
    else:
        raise ValueError(f"unknown strategy {strategy!r}")

    for f in engine.fills:
        print(f"  {f.side:4s}  price={f.price:>12.2f}  qty={f.qty:.6f}  fee={f.fee:.4f}")

    result = engine.summary(last_price)
    print("\n--- Result (SIMULATED, no real orders were placed) ---")
    for k, v in result.items():
        print(f"  {k}: {v}")

    if strategy == "walk-forward":
        start_close = float(klines[0]["close"])
        buy_hold_pct = (last_price - start_close) / start_close * 100
        print(f"\n  (for comparison) BTC buy-and-hold over same window: {buy_hold_pct:+.2f}%")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default="BTC_USDT")
    parser.add_argument("--grids", type=int, default=8)
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--interval", default="1D",
                         choices=["1M", "5M", "15M", "30M", "60M", "4H", "8H", "12H", "1D"])
    parser.add_argument("--limit", type=int, default=200, help="number of bars, max 500")
    parser.add_argument("--end-date", default=None,
                         help="YYYY-MM-DD (UTC), backtest a historical window ending here "
                              "instead of the most recent bars")
    parser.add_argument("--strategy", default="walk-forward",
                         choices=["walk-forward", "trend-filtered", "baseline"])
    parser.add_argument("--lower", type=float, default=None,
                         help="required for --strategy trend-filtered/baseline")
    parser.add_argument("--upper", type=float, default=None,
                         help="required for --strategy trend-filtered/baseline")
    parser.add_argument("--sma-period", type=int, default=30)
    parser.add_argument("--deviation-stop", type=float, default=0.05,
                         help="fraction below SMA that triggers a defensive exit, e.g. 0.05 = 5%")
    parser.add_argument("--spacing", default="geometric", choices=["geometric", "arithmetic"])
    parser.add_argument("--lookback-days", type=int, default=30,
                         help="walk-forward only: trailing days used to set the grid range")
    parser.add_argument("--buffer-low", type=float, default=0.95,
                         help="walk-forward only: lower bound = trailing low * this")
    parser.add_argument("--buffer-high", type=float, default=1.10,
                         help="walk-forward only: upper bound = trailing high * this")
    parser.add_argument("--recalibrate-every", type=int, default=7,
                         help="walk-forward only: bars between range recalibrations")
    args = parser.parse_args()

    run(args.symbol, args.grids, args.capital, args.interval, args.limit, args.end_date,
        args.strategy, args.lower, args.upper, args.sma_period, args.deviation_stop,
        args.spacing, args.lookback_days, args.buffer_low, args.buffer_high,
        args.recalibrate_every)
