"""Read-only market/account watcher -- sends LINE push notifications, never
places or affects any trade. Meant to run as a one-shot script every 15 min
via Windows Task Scheduler (not a long-running loop).

Three notification types, each fired at most once per "event" (state is
persisted in notify_watch_state.json so a 15-min re-run doesn't spam):

1. Tier signal change (0% <-> 50% <-> 100%) -- purely computed from public
   BTC/USDT daily klines, same SMA(50/100/200) logic as live_ta.py's
   LiveTieredMATrader, but this script never touches the account and never
   trades. Informational only.
2. "Clean entry point" -- tier is 100% AND close is within 8% of SMA200 AND
   MA5 > MA10. Threshold validated against the full 2017-2026 BTC history
   (see project memory/README for the backtest that grounds the 8% figure).
   Fires once when the condition newly becomes true, resets when it goes
   false again so it can re-fire later.
3. Portfolio stop-loss triggered -- read from live_status.json (written by
   live_ta.py itself), detected via running=False + a message starting with
   "PORTFOLIO STOP-LOSS HIT". This is the one event that reflects something
   that actually already happened to the account, not just a market signal.

None of this ever calls place_order or touches PIONEX_API_KEY/SECRET --
notification types 1 and 2 only need public market data; type 3 only reads
a status file live_ta.py already writes for the dashboard.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pionex_client import PionexClient
from grid_strategy import sma

STATE_FILE = Path(__file__).parent / "notify_watch_state.json"
STATUS_FILE = Path(__file__).parent / "live_status.json"
LINE_ENV_PATH = Path.home() / ".claude" / "secrets" / "line_notify.env"

SYMBOL = "BTC_USDT"
MA_SHORT, MA_MID, MA_LONG = 50, 100, 200
CLEAN_ENTRY_EXT_THRESHOLD = 0.08  # validated: ext<=8% + MA5>MA10 -> mean +32.4% / median +28.0% fwd 90d (n=79)


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _load_line_env() -> dict:
    values = {}
    try:
        with open(LINE_ENV_PATH, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key] = value
    except FileNotFoundError:
        pass
    return values


def line_push(text: str) -> None:
    env = _load_line_env()
    token = env.get("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = env.get("LINE_MY_USER_ID")
    if not token or not user_id:
        print("LINE credentials not found, skipping push:", text)
        return
    body = json.dumps({
        "to": user_id,
        "messages": [{"type": "text", "text": text[:2000]}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print("LINE push failed:", e)


def compute_signal() -> dict:
    client = PionexClient()  # public data only, no credentials needed
    limit = MA_LONG + 15
    klines = client.get_klines(SYMBOL, interval="1D", limit=limit)
    klines = sorted(klines, key=lambda k: int(k["time"]))
    closes = [float(k["close"]) for k in klines]
    i = len(closes) - 1
    close = closes[i]
    s_short = sma(closes, i, MA_SHORT)
    s_mid = sma(closes, i, MA_MID)
    s_long = sma(closes, i, MA_LONG)
    ma5 = sma(closes, i, 5)
    ma10 = sma(closes, i, 10)

    if s_long is None:
        return {"tier": None}

    if s_short is not None and s_mid is not None and close > s_short and close > s_mid and close > s_long:
        tier = 1.0
    elif close > s_long:
        tier = 0.5
    else:
        tier = 0.0

    ext200 = close / s_long - 1
    momentum = ma5 is not None and ma10 is not None and ma5 > ma10
    clean_entry = tier == 1.0 and ext200 <= CLEAN_ENTRY_EXT_THRESHOLD and momentum

    return {"tier": tier, "close": close, "ext200": ext200, "momentum": momentum,
             "clean_entry": clean_entry}


TIER_LABEL = {0.0: "0%（空手）", 0.5: "50%", 1.0: "100%（全倉）"}


def check_tier_change(state: dict, sig: dict) -> None:
    if sig["tier"] is None:
        return
    last_tier = state.get("last_tier")
    if last_tier is None:
        state["last_tier"] = sig["tier"]  # first run: just record, don't notify
        return
    if sig["tier"] != last_tier:
        line_push(
            f"[Pionex監控] \U0001f504 倉位訊號變化\n"
            f"{TIER_LABEL.get(last_tier, last_tier)} → {TIER_LABEL.get(sig['tier'], sig['tier'])}\n"
            f"BTC/USDT 收盤 ${sig['close']:,.0f}\n\n"
            f"僅供參考，live_ta.py 會依自己的邏輯獨立判斷、獨立下單。"
        )
        state["last_tier"] = sig["tier"]


def check_clean_entry(state: dict, sig: dict) -> None:
    if sig["tier"] is None:
        return
    was_notified = state.get("clean_entry_notified", False)
    if sig["clean_entry"] and not was_notified:
        line_push(
            f"[Pionex監控] \U0001f4ca 乾淨進場點出現\n"
            f"BTC/USDT 收盤 ${sig['close']:,.0f}\n"
            f"乖離SMA200：{sig['ext200']:+.1%}（門檻 ≤{CLEAN_ENTRY_EXT_THRESHOLD:.0%}）\n"
            f"短線動能 MA5>MA10：是\n\n"
            f"僅供參考，不會自動下單，live_ta.py 照原本邏輯獨立運作。"
        )
        state["clean_entry_notified"] = True
    elif not sig["clean_entry"]:
        state["clean_entry_notified"] = False


def check_stop_loss(state: dict) -> None:
    try:
        status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return
    running = status.get("running")
    last_message = status.get("last_message", "")
    was_notified = state.get("stoploss_notified", False)
    if running is False and last_message.startswith("PORTFOLIO STOP-LOSS HIT") and not was_notified:
        line_push(
            f"[Pionex監控] \U0001f6d1 整體停損已觸發\n"
            f"權益 ${status.get('equity', 0):,.2f}，較最高點 ${status.get('peak_equity', 0):,.2f} "
            f"回落 {status.get('drawdown_pct', 0):.1f}%\n"
            f"已全部賣出並永久停止程式，不會自動重啟\n\n"
            f"請自行確認派網帳戶狀態。"
        )
        state["stoploss_notified"] = True
    elif running is True:
        state["stoploss_notified"] = False


def main() -> None:
    state = _load_state()
    try:
        sig = compute_signal()
    except Exception as e:
        print("Failed to compute signal (network/API issue), skipping this run:", e)
        return
    check_tier_change(state, sig)
    check_clean_entry(state, sig)
    check_stop_loss(state)
    _save_state(state)


if __name__ == "__main__":
    main()
