"""Grid-trading engine: pure logic, no exchange calls.

Risk defaults below come from the two-round AI-council review (Groq +
Gemini, converged) for a small-capital, simulate-first user:
  - capital per grid line: 3-5% of total capital
  - max concurrent grid lines open: 20-30% of total capital deployed
  - profit per grid: 0.5-1.2% (after fees)
  - portfolio-level stop loss: 15-20% drawdown from starting capital
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GridConfig:
    symbol: str
    lower_price: float
    upper_price: float
    grid_count: int
    total_capital: float
    fee_rate: float = 0.0005  # 0.05% taker, adjust to your actual Pionex fee tier
    capital_per_grid_pct: float = 0.04  # 4% of total capital per line (within 3-5%)
    max_concurrent_grids_pct: float = 0.25  # 25% of capital deployed at once (within 20-30%)
    portfolio_stop_loss_pct: float = 0.18  # 18% drawdown triggers full flatten (within 15-20%)
    spacing: str = "geometric"  # "geometric" (fixed % step, default) or "arithmetic" (fixed price step)
    # geometric backtested 2026-08-31 as a small but consistent improvement over
    # arithmetic across all 4 historical regimes (+0.86pp aggregate) -- see README.md

    def __post_init__(self):
        if self.upper_price <= self.lower_price:
            raise ValueError("upper_price must be > lower_price")
        if self.grid_count < 2:
            raise ValueError("grid_count must be >= 2")
        if self.spacing not in ("arithmetic", "geometric"):
            raise ValueError("spacing must be 'arithmetic' or 'geometric'")

    @property
    def grid_lines(self) -> list[float]:
        if self.spacing == "geometric":
            # equal PERCENTAGE step between lines: price[i] = lower * ratio**i,
            # so lines cluster tighter at the low end and wider at the high end
            ratio = (self.upper_price / self.lower_price) ** (1 / self.grid_count)
            return [self.lower_price * (ratio ** i) for i in range(self.grid_count + 1)]
        step = (self.upper_price - self.lower_price) / self.grid_count
        return [self.lower_price + i * step for i in range(self.grid_count + 1)]

    @property
    def capital_per_grid(self) -> float:
        return self.total_capital * self.capital_per_grid_pct

    @property
    def max_concurrent_capital(self) -> float:
        return self.total_capital * self.max_concurrent_grids_pct


@dataclass
class Fill:
    timestamp: int
    price: float
    side: str  # BUY or SELL
    qty: float
    fee: float


@dataclass
class GridEngine:
    """Simulates a spot grid strategy against a stream of prices.

    Model: each grid line holds at most one open "slot". When price crosses
    a line moving down through it, we buy one unit of capital_per_grid at
    that line. When price later crosses back up through the line above the
    one we bought at, we sell (capturing the per-grid profit). This mirrors
    how Pionex's own grid bot pairs buy/sell orders at adjacent lines.
    """
    config: GridConfig
    cash: float = field(init=False)
    position_qty: float = field(init=False, default=0.0)
    open_lines: dict[int, float] = field(init=False, default_factory=dict)  # line_idx -> qty bought
    fills: list[Fill] = field(init=False, default_factory=list)
    starting_capital: float = field(init=False)
    stopped: bool = field(init=False, default=False)
    stop_reason: str | None = field(init=False, default=None)
    buying_enabled: bool = field(init=False, default=True)

    def __post_init__(self):
        self.cash = self.config.total_capital
        self.starting_capital = self.config.total_capital

    def _deployed_capital(self) -> float:
        return sum(qty * self.config.grid_lines[idx] for idx, qty in self.open_lines.items())

    def _equity(self, last_price: float) -> float:
        return self.cash + self.position_qty * last_price

    def set_buying_enabled(self, enabled: bool) -> None:
        """Gate new BUYs on/off without touching existing resting sells or
        the portfolio stop-loss. Used by run_trend_filtered() to pause
        entries while price is below its trend filter."""
        self.buying_enabled = enabled

    def close_all_positions(self, timestamp: int, price: float, reason: str | None = None) -> None:
        """Sell everything at `price` and clear tracked grid lines, but keep
        the engine running (unlike _flatten, which also permanently stops
        it). Used for a defensive exit that may resume trading later."""
        if self.position_qty > 0:
            proceeds = self.position_qty * price
            fee = proceeds * self.config.fee_rate
            self.cash += (proceeds - fee)
            self.fills.append(Fill(timestamp, price, "SELL", self.position_qty, fee))
            self.position_qty = 0.0
        self.open_lines.clear()

    def on_price(self, timestamp: int, price: float) -> None:
        if self.stopped:
            return

        lines = self.config.grid_lines
        equity = self._equity(price)
        drawdown = (self.starting_capital - equity) / self.starting_capital
        if drawdown >= self.config.portfolio_stop_loss_pct:
            self._flatten(timestamp, price, f"portfolio stop-loss hit ({drawdown:.1%} drawdown)")
            return

        if price < self.config.lower_price or price > self.config.upper_price:
            return  # outside grid range, no action (matches Pionex grid bot idle behavior)

        for idx in range(len(lines) - 1):
            buy_line = lines[idx]
            sell_line = lines[idx + 1]

            # price dipped to/through a buy line we don't already hold -> buy
            if price <= buy_line and idx not in self.open_lines and self.buying_enabled:
                if self._deployed_capital() + self.config.capital_per_grid > self.config.max_concurrent_capital:
                    continue  # respect max concurrent grid exposure
                qty = self.config.capital_per_grid / buy_line
                cost = qty * buy_line
                fee = cost * self.config.fee_rate
                if cost + fee > self.cash:
                    continue
                self.cash -= (cost + fee)
                self.position_qty += qty
                self.open_lines[idx] = qty
                self.fills.append(Fill(timestamp, buy_line, "BUY", qty, fee))

            # price rose to/through the sell line for a line we hold -> sell
            if price >= sell_line and idx in self.open_lines:
                qty = self.open_lines.pop(idx)
                proceeds = qty * sell_line
                fee = proceeds * self.config.fee_rate
                self.cash += (proceeds - fee)
                self.position_qty -= qty
                self.fills.append(Fill(timestamp, sell_line, "SELL", qty, fee))

    def _flatten(self, timestamp: int, price: float, reason: str) -> None:
        """Permanent stop: closes everything and halts the engine for good."""
        self.close_all_positions(timestamp, price, reason)
        self.stopped = True
        self.stop_reason = reason

    def summary(self, last_price: float) -> dict:
        equity = self._equity(last_price)
        realized_pnl = sum(
            (f.price * f.qty - f.fee) if f.side == "SELL" else -(f.price * f.qty + f.fee)
            for f in self.fills
        )
        return {
            "fills": len(self.fills),
            "buys": sum(1 for f in self.fills if f.side == "BUY"),
            "sells": sum(1 for f in self.fills if f.side == "SELL"),
            "cash": round(self.cash, 2),
            "position_qty": round(self.position_qty, 8),
            "equity": round(equity, 2),
            "pnl": round(equity - self.starting_capital, 2),
            "pnl_pct": round((equity - self.starting_capital) / self.starting_capital * 100, 2),
            "stopped": self.stopped,
            "stop_reason": self.stop_reason,
        }


def sma(closes: list[float], idx: int, period: int) -> float | None:
    if idx < period - 1:
        return None
    return sum(closes[idx - period + 1: idx + 1]) / period


def run_trend_filtered(klines: list[dict], config: GridConfig,
                        sma_period: int = 30,
                        deviation_stop_pct: float = 0.05) -> tuple[GridEngine, float]:
    """Trend-filtered grid: pauses new BUYs while close < SMA(sma_period),
    and force-closes the position if close falls more than
    deviation_stop_pct below the SMA (defensive exit ahead of a possible
    larger decline). The existing portfolio-level stop-loss still applies
    on top of this.

    Backtested 2026-08-31 against 4 real BTC/USDT historical regimes and
    cross-checked by the llm-council AI team (2 rounds, Groq/Gemini/
    OpenRouter converged): turned a -10.1% crash-scenario loss into -0.45%,
    and improved the 4-scenario aggregate from -5.26% to +1.12%, at the
    cost of a small defensive-exit drag (-0.84%) in a topping/choppy
    scenario that preceded the crash. See README.md "策略研究" section.

    Caveat: SMA(30) is a compromise forced by our backtest dataset only
    having ~500 daily bars total (SMA(200), which the AI team originally
    proposed, would leave too little lookback). SMA(30) behaves more like a
    fast regime-switch than a true long-term trend filter, and is more
    prone to whipsaws in a prolonged sideways market we haven't tested
    against. Revisit with SMA(50-100) once more history is available.
    """
    closes = [float(k["close"]) for k in klines]
    engine = GridEngine(config)
    last_price = closes[0] if closes else 0.0

    for i, k in enumerate(klines):
        ts = int(k["time"])
        close = closes[i]
        trend = sma(closes, i, sma_period)

        if trend is not None:
            engine.set_buying_enabled(close >= trend)
            if close < trend * (1 - deviation_stop_pct) and engine.position_qty > 0:
                engine.close_all_positions(
                    ts, close, f"trend filter: >{deviation_stop_pct:.0%} below SMA({sma_period})"
                )

        for price in (float(k["open"]), float(k["low"]), float(k["high"]), float(k["close"])):
            engine.on_price(ts, price)
            last_price = price
        if engine.stopped:
            break

    return engine, last_price


def run_walk_forward(klines: list[dict], grid_count: int, total_capital: float,
                      lookback_days: int = 30, buffer_low: float = 0.95,
                      buffer_high: float = 1.10, recalibrate_every: int = 7,
                      sma_period: int = 30, deviation_stop_pct: float = 0.05,
                      fee_rate: float = 0.0005) -> tuple[GridEngine, float, list[dict]]:
    """Grid range is NOT set by the caller -- it's recomputed every
    `recalibrate_every` bars from ONLY the trailing `lookback_days` of
    high/low (never future data), as [recent_low*buffer_low,
    recent_high*buffer_high]. Combined with the SMA trend filter and
    geometric spacing. This is the honest, no-look-ahead counterpart to
    run_trend_filtered() (which requires the caller to hand-pick a fixed
    range -- fine for a known symbol you're actively managing, but easy to
    accidentally bias with hindsight in a backtest).

    Needs at least `lookback_days` bars of history before trading can
    start (no range to trade against before that). Requires len(klines) >
    lookback_days + a few bars, or nothing will ever recalibrate.

    Backtested 2026-08-31 over the full ~500-day BTC/USDT history available
    (continuous single run, not independently-reset scenarios): -7.0%,
    vs -8.93% for simple buy-and-hold over the identical window -- i.e. it
    lost less than holding during a rough stretch for BTC, not that it was
    profitable in absolute terms. Don't expect this to reliably beat a
    strong uptrend; its edge (in this one backtest) was capital
    preservation during chop/decline, not growth. See README.md.
    """
    closes = [float(k["close"]) for k in klines]
    highs = [float(k["high"]) for k in klines]
    lows = [float(k["low"]) for k in klines]

    config = GridConfig(symbol="", lower_price=1, upper_price=2, grid_count=grid_count,
                         total_capital=total_capital, fee_rate=fee_rate, spacing="geometric")
    engine = GridEngine(config)
    last_recalib = -999
    last_price = closes[0] if closes else 0.0
    equity_curve = []

    for i, k in enumerate(klines):
        ts = int(k["time"])
        close = closes[i]
        last_price = close

        trend = sma(closes, i, sma_period)
        if trend is not None:
            engine.set_buying_enabled(close >= trend)
            if close < trend * (1 - deviation_stop_pct) and engine.position_qty > 0:
                engine.close_all_positions(ts, close, "trend filter deviation stop")

        if (not engine.open_lines and (i - last_recalib) >= recalibrate_every
                and i - lookback_days >= 0):
            recent_low = min(lows[i - lookback_days:i])
            recent_high = max(highs[i - lookback_days:i])
            new_lower, new_upper = recent_low * buffer_low, recent_high * buffer_high
            if new_upper > new_lower:
                engine.config.lower_price = new_lower
                engine.config.upper_price = new_upper
                last_recalib = i

        if close < engine.config.lower_price and engine.position_qty > 0:
            engine.close_all_positions(ts, close, "broke below current grid range")

        for price in (float(k["open"]), lows[i], highs[i], close):
            engine.on_price(ts, price)
        equity_curve.append({"time": ts, "close": close, "equity": engine._equity(close)})
        if engine.stopped:
            break

    return engine, last_price, equity_curve
