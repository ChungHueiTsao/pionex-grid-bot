"""Generates the monthly research report: re-backtests all strategies
against the latest available BTC/USDT history and renders docs/index.html
(a static site for GitHub Pages) plus appends a snapshot to
docs/history.json so the site can show how each strategy's live-forward
numbers evolve over time, not just a single frozen backtest.

No API key needed (public market data only). Never places any order.
Run manually (`python generate_report.py`) or via the monthly scheduled
task; either way it only writes files in docs/ -- committing and pushing
to GitHub is a separate, explicit step (see README "定期報告與排程" section).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from grid_strategy import run_walk_forward
from ta_strategy import run_tiered_ma_strategy, run_ma_filter_strategy, run_ta_strategy
from pionex_client import PionexClient

DOCS_DIR = Path(__file__).parent / "docs"
HISTORY_FILE = DOCS_DIR / "history.json"
TEMPLATE_FILE = Path(__file__).parent / "report_template.html"
CAPITAL = 1000.0


def fetch_full_history(client: PionexClient, symbol: str = "BTC_USDT", max_chunks: int = 10) -> list[dict]:
    cursor_end = None
    all_bars: list[dict] = []
    for _ in range(max_chunks):
        kw = {} if cursor_end is None else {"end_time": cursor_end - 1}
        chunk = client.get_klines(symbol, interval="1D", limit=500, **kw)
        if not chunk:
            break
        chunk = sorted(chunk, key=lambda k: int(k["time"]))
        all_bars = chunk + all_bars
        cursor_end = int(chunk[0]["time"])
        if len(chunk) < 500:
            break
    seen = {int(k["time"]): k for k in all_bars}
    return [seen[t] for t in sorted(seen)]


def max_drawdown(equities: list[float]) -> float:
    peak = CAPITAL
    mdd = 0.0
    for eq in equities:
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    return mdd


def run_report() -> dict:
    client = PionexClient()
    klines = fetch_full_history(client)
    closes = [float(k["close"]) for k in klines]
    dates = [datetime.fromtimestamp(int(k["time"]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
             for k in klines]

    tiered_engine, tiered_last, tiered_curve = run_tiered_ma_strategy(klines, CAPITAL)
    ma_engine, ma_last, ma_curve = run_ma_filter_strategy(klines, CAPITAL)
    grid_engine, grid_last, grid_curve = run_walk_forward(klines, grid_count=8, total_capital=CAPITAL)
    emaatr_engine, emaatr_last, emaatr_curve = run_ta_strategy(klines, CAPITAL)

    start_close = closes[0]
    buy_hold_equity = [CAPITAL * (c / start_close) for c in closes]

    def pct(final_equity: float) -> float:
        return round((final_equity - CAPITAL) / CAPITAL * 100, 2)

    tiered_eq = [c["equity"] for c in tiered_curve]
    ma_eq = [c["equity"] for c in ma_curve]
    grid_eq = [c["equity"] for c in grid_curve] + [grid_curve[-1]["equity"]] * (len(klines) - len(grid_curve))
    emaatr_eq = [c["equity"] for c in emaatr_curve]

    summary = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "period_start": dates[0],
        "period_end": dates[-1],
        "days": len(klines),
        "buy_hold_pct": pct(buy_hold_equity[-1]),
        "buy_hold_mdd": round(max_drawdown(buy_hold_equity) * 100, 1),
        "tiered_ma_pct": pct(tiered_eq[-1]),
        "tiered_ma_mdd": round(max_drawdown(tiered_eq) * 100, 1),
        "tiered_ma_trades": len(tiered_engine.fills),
        "ma_filter_pct": pct(ma_eq[-1]),
        "ma_filter_mdd": round(max_drawdown(ma_eq) * 100, 1),
        "ma_filter_trades": len(ma_engine.fills),
        "grid_pct": pct(grid_eq[-1]),
        "grid_mdd": round(max_drawdown(grid_eq) * 100, 1),
        "grid_trades": len(grid_engine.fills),
        "emaatr_pct": pct(emaatr_eq[-1]),
        "emaatr_mdd": round(max_drawdown(emaatr_eq) * 100, 1),
        "emaatr_trades": len(emaatr_engine.fills),
    }

    chart_data = {
        "dates": dates,
        "buy_hold_equity": buy_hold_equity,
        "tiered_ma_equity": tiered_eq,
        "ma_filter_equity": ma_eq,
        "grid_equity": grid_eq,
        "emaatr_equity": emaatr_eq,
        "summary": summary,
    }
    return chart_data


def append_history(summary: dict) -> list[dict]:
    DOCS_DIR.mkdir(exist_ok=True)
    history = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else []
    run_date = summary["generated_at"][:10]
    # replace today's entry if this is a re-run on the same day, else append
    history = [h for h in history if h["date"] != run_date]
    history.append({
        "date": run_date,
        "tiered_ma_pct": summary["tiered_ma_pct"],
        "ma_filter_pct": summary["ma_filter_pct"],
        "grid_pct": summary["grid_pct"],
        "emaatr_pct": summary["emaatr_pct"],
        "buy_hold_pct": summary["buy_hold_pct"],
        "tiered_ma_mdd": summary["tiered_ma_mdd"],
    })
    HISTORY_FILE.write_text(json.dumps(history, indent=None))
    return history


def render_html(chart_data: dict, history: list[dict]) -> None:
    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    out = template.replace("__CHART_DATA__", json.dumps(chart_data))
    out = out.replace("__HISTORY_DATA__", json.dumps(history))
    (DOCS_DIR / "index.html").write_text(out, encoding="utf-8")


if __name__ == "__main__":
    print("Fetching latest BTC/USDT history and re-running all backtests...")
    data = run_report()
    print(f"Period: {data['summary']['period_start']} -> {data['summary']['period_end']} "
          f"({data['summary']['days']} days)")
    for name in ("buy_hold", "tiered_ma", "ma_filter", "grid", "emaatr"):
        print(f"  {name}: {data['summary'][name + '_pct']:+.1f}%  "
              f"MDD={data['summary'][name + '_mdd']:.1f}%")

    hist = append_history(data["summary"])
    render_html(data, hist)
    print(f"\nWrote {DOCS_DIR / 'index.html'} and {HISTORY_FILE}")
    print("Review the output, then commit + push to update the live site:")
    print("  git add docs/ && git commit -m 'Monthly report update' && git push")
