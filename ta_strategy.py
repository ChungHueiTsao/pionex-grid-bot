"""Pure technical-indicator trend-following strategy -- no grid mechanics.

Synthesized from an AI-council round (Groq/Gemini/OpenRouter all converged
on "trend-follow + ATR-based stop/take-profit"; this is Claude's own
concrete implementation, correcting one model's apparently inverted
crossover description against the standard convention: fast EMA crossing
above slow EMA is the bullish signal, not the reverse).

Rules (long-only, single position at a time -- spot, no shorting/leverage):
  - EMA(20) crosses above EMA(50) ("golden cross") and flat -> BUY with
    `position_pct` of cash. Record stop = entry - stop_mult*ATR(14),
    take-profit = entry + tp_mult*ATR(14).
  - While holding, SELL (whichever hits first):
      * close <= stop price
      * close >= take-profit price
      * EMA(20) crosses back below EMA(50) ("death cross")
  - No other logic. Everything is computed causally (each bar only uses
    its own and earlier bars) so this is walk-forward safe by construction.

ATR here is a simple-moving-average of True Range (not Wilder's smoothed
ATR) -- an approximation, close enough for this purpose but worth knowing
if you compare numbers against a charting platform's ATR.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def ema_series(closes: list[float], period: int) -> list[float | None]:
    """Causal EMA: index i only depends on values[0..i]. Seeded with the
    SMA of the first `period` closes."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return out
    alpha = 2 / (period + 1)
    seed = sum(closes[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(closes)):
        prev = alpha * closes[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def atr_series(highs: list[float], lows: list[float], closes: list[float],
                period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    trs: list[float] = [0.0] * len(closes)
    for i in range(1, len(closes)):
        trs[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
    for i in range(period, len(closes)):
        out[i] = sum(trs[i - period + 1: i + 1]) / period
    return out


@dataclass
class Fill:
    timestamp: int
    price: float
    side: str
    qty: float
    fee: float
    reason: str


@dataclass
class TAEngine:
    starting_capital: float
    fee_rate: float = 0.0005
    cash: float = field(init=False)
    position_qty: float = field(init=False, default=0.0)
    entry_price: float | None = field(init=False, default=None)
    stop_price: float | None = field(init=False, default=None)
    take_profit_price: float | None = field(init=False, default=None)
    fills: list[Fill] = field(init=False, default_factory=list)

    def __post_init__(self):
        self.cash = self.starting_capital

    def equity(self, price: float) -> float:
        return self.cash + self.position_qty * price

    def summary(self, last_price: float) -> dict:
        equity = self.equity(last_price)
        return {
            "fills": len(self.fills),
            "buys": sum(1 for f in self.fills if f.side == "BUY"),
            "sells": sum(1 for f in self.fills if f.side == "SELL"),
            "cash": round(self.cash, 2),
            "position_qty": round(self.position_qty, 8),
            "equity": round(equity, 2),
            "pnl": round(equity - self.starting_capital, 2),
            "pnl_pct": round((equity - self.starting_capital) / self.starting_capital * 100, 2),
        }


def run_ta_strategy(klines: list[dict], total_capital: float, fee_rate: float = 0.0005,
                     ema_fast: int = 20, ema_slow: int = 50, atr_period: int = 14,
                     stop_mult: float = 2.0, tp_mult: float = 3.0,
                     position_pct: float = 0.95) -> tuple[TAEngine, float, list[dict]]:
    """Returns (engine, last_price, equity_curve) where equity_curve is a
    list of {"time": ms, "close": float, "equity": float} for charting."""
    closes = [float(k["close"]) for k in klines]
    highs = [float(k["high"]) for k in klines]
    lows = [float(k["low"]) for k in klines]
    times = [int(k["time"]) for k in klines]

    fast = ema_series(closes, ema_fast)
    slow = ema_series(closes, ema_slow)
    atr = atr_series(highs, lows, closes, atr_period)

    engine = TAEngine(total_capital, fee_rate)
    equity_curve = []
    last_price = closes[0] if closes else 0.0

    for i in range(len(klines)):
        close = closes[i]
        last_price = close
        if (fast[i] is None or slow[i] is None or atr[i] is None or i == 0
                or fast[i - 1] is None or slow[i - 1] is None):
            equity_curve.append({"time": times[i], "close": close, "equity": engine.equity(close)})
            continue

        golden_cross = fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]
        death_cross = fast[i] < slow[i] and fast[i - 1] >= slow[i - 1]

        if engine.position_qty == 0.0 and golden_cross:
            spend = engine.cash * position_pct
            qty = spend / close
            fee = spend * fee_rate
            engine.cash -= (spend + fee)
            engine.position_qty = qty
            engine.entry_price = close
            engine.stop_price = close - stop_mult * atr[i]
            engine.take_profit_price = close + tp_mult * atr[i]
            engine.fills.append(Fill(times[i], close, "BUY", qty, fee, "golden cross entry"))

        elif engine.position_qty > 0.0:
            reason = None
            if close <= engine.stop_price:
                reason = "stop-loss"
            elif close >= engine.take_profit_price:
                reason = "take-profit"
            elif death_cross:
                reason = "trend reversal (death cross)"
            if reason:
                proceeds = engine.position_qty * close
                fee = proceeds * fee_rate
                engine.fills.append(Fill(times[i], close, "SELL", engine.position_qty, fee, reason))
                engine.cash += (proceeds - fee)
                engine.position_qty = 0.0
                engine.entry_price = engine.stop_price = engine.take_profit_price = None

        equity_curve.append({"time": times[i], "close": close, "equity": engine.equity(close)})

    return engine, last_price, equity_curve


def run_tiered_ma_strategy(klines: list[dict], total_capital: float, fee_rate: float = 0.0005,
                            ma_short: int = 50, ma_mid: int = 100,
                            ma_long: int = 200) -> tuple[TAEngine, float, list[dict]]:
    """Position-size in tiers off three SMAs instead of a single binary
    on/off switch, to reduce whipsaw right at the crossing point:
      - close > SMA(short) AND > SMA(mid) AND > SMA(long) -> target 100% invested
      - close > SMA(long) only (below one of the shorter MAs)  -> target 50%
      - close < SMA(long)                                      -> target 0%
    Only trades the *delta* needed to move to a new target when the tier
    changes (not every bar), to keep fee churn down.

    AI-council synthesized (Groq's MT-MA-Scale + Gemini's Triple-MA
    Scaling both converged on this shape). Backtested 2026-08-31 over the
    full ~9-year BTC/USDT history: see README.md for the number and how it
    compares to run_ma_filter_strategy() (the plain single-200-day-SMA
    version, previously the best result).
    """
    from grid_strategy import sma

    closes = [float(k["close"]) for k in klines]
    times = [int(k["time"]) for k in klines]

    engine = TAEngine(total_capital, fee_rate)
    equity_curve = []
    last_price = closes[0] if closes else 0.0
    current_tier = 0.0

    for i in range(len(klines)):
        close = closes[i]
        last_price = close
        s_short = sma(closes, i, ma_short)
        s_mid = sma(closes, i, ma_mid)
        s_long = sma(closes, i, ma_long)

        if s_long is not None:
            if s_short is not None and s_mid is not None and close > s_short and close > s_mid and close > s_long:
                target_tier = 1.0
            elif close > s_long:
                target_tier = 0.5
            else:
                target_tier = 0.0

            if target_tier != current_tier:
                equity_now = engine.equity(close)
                target_value = equity_now * target_tier
                current_value = engine.position_qty * close
                diff_value = target_value - current_value

                if diff_value > 0:  # need to buy more
                    spend = min(diff_value, engine.cash)
                    if spend > 0:
                        qty = spend / close
                        fee = spend * fee_rate
                        engine.cash -= (spend + fee)
                        engine.position_qty += qty
                        engine.fills.append(Fill(times[i], close, "BUY", qty, fee,
                                                  f"tier {current_tier:.0%}->{target_tier:.0%}"))
                elif diff_value < 0:  # need to sell some
                    sell_value = min(-diff_value, current_value)
                    qty = sell_value / close if close > 0 else 0
                    qty = min(qty, engine.position_qty)
                    proceeds = qty * close
                    fee = proceeds * fee_rate
                    engine.cash += (proceeds - fee)
                    engine.position_qty -= qty
                    engine.fills.append(Fill(times[i], close, "SELL", qty, fee,
                                              f"tier {current_tier:.0%}->{target_tier:.0%}"))
                current_tier = target_tier

        equity_curve.append({"time": times[i], "close": close, "equity": engine.equity(close)})

    return engine, last_price, equity_curve


def run_ma_filter_strategy(klines: list[dict], total_capital: float, fee_rate: float = 0.0005,
                            ma_period: int = 200,
                            position_pct: float = 0.95) -> tuple[TAEngine, float, list[dict]]:
    """Simplest possible trend filter: all-in when close > SMA(ma_period),
    all-out when close < SMA(ma_period). No ATR stop-loss/take-profit at
    all -- long-only, single position, patient.

    Backtested 2026-08-31 over the full ~9-year BTC/USDT history available
    (2017-10-26 to 2026-08-31, single continuous compounding run):
    +1003.4% vs +1228.5% for buy-and-hold over the same window, but with
    max drawdown of 62.7% vs buy-and-hold's 83.5%. This captured ~96% of
    buy-and-hold's return while meaningfully cutting the worst drawdown --
    a much better risk-adjusted outcome than run_ta_strategy() (EMA+ATR),
    which lost to buy-and-hold outright (-4.4%) over the same period.

    The lesson from comparing the two: run_ta_strategy()'s 2x-ATR stop was
    tight enough to repeatedly whipsaw out of real uptrends on BTC's
    volatility -- removing the tight stop and using only a slow, patient
    trend filter did better than adding "smarter" risk management. See
    README.md.
    """
    from grid_strategy import sma  # local import to avoid a hard module-level dependency

    closes = [float(k["close"]) for k in klines]
    times = [int(k["time"]) for k in klines]

    engine = TAEngine(total_capital, fee_rate)
    equity_curve = []
    last_price = closes[0] if closes else 0.0

    for i in range(len(klines)):
        close = closes[i]
        last_price = close
        trend = sma(closes, i, ma_period)

        if trend is not None:
            if engine.position_qty == 0.0 and close > trend:
                spend = engine.cash * position_pct
                qty = spend / close
                fee = spend * fee_rate
                engine.cash -= (spend + fee)
                engine.position_qty = qty
                engine.fills.append(Fill(times[i], close, "BUY", qty, fee, f"close > SMA({ma_period})"))
            elif engine.position_qty > 0.0 and close < trend:
                proceeds = engine.position_qty * close
                fee = proceeds * fee_rate
                engine.fills.append(Fill(times[i], close, "SELL", engine.position_qty, fee,
                                          f"close < SMA({ma_period})"))
                engine.cash += (proceeds - fee)
                engine.position_qty = 0.0

        equity_curve.append({"time": times[i], "close": close, "equity": engine.equity(close)})

    return engine, last_price, equity_curve
