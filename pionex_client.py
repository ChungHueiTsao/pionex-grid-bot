"""Pionex REST API client.

Auth scheme per official docs (https://www.pionex.com/docs/api-docs/trade-api/):
  - headers: PIONEX-KEY (api key), PIONEX-SIGNATURE (hex HMAC-SHA256)
  - every private request carries a `timestamp` query param (ms, +/- 20s window)
  - signature string = METHOD + PATH + "?" + sorted_query_string(&-joined, incl. timestamp)
    + (raw JSON body, only for POST/DELETE)
  - query params are sorted alphabetically by key, no URL-encoding in the signed string
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

import requests

BASE_URL = "https://api.pionex.com"


class PionexAPIError(RuntimeError):
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"Pionex API error {status_code}: {payload}")


@dataclass
class Balance:
    coin: str
    free: float
    frozen: float


class PionexClient:
    """Thin wrapper. Public methods need no credentials; private methods do.

    Instantiate with api_key=None, api_secret=None to use only the public
    market-data methods (get_klines / get_ticker / get_depth) -- that's all
    the simulator needs.
    """

    def __init__(self, api_key: str | None = None, api_secret: str | None = None,
                 timeout: float = 10.0):
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout
        self._session = requests.Session()
        self._symbol_info_cache: dict[str, dict] = {}

    # ---- signing -----------------------------------------------------

    def _sorted_query(self, params: dict[str, Any]) -> str:
        items = sorted((k, v) for k, v in params.items() if v is not None)
        return "&".join(f"{k}={v}" for k, v in items)

    def _sign(self, method: str, path: str, query: str, body: str) -> str:
        if not self.api_secret:
            raise RuntimeError("api_secret is required for private endpoints")
        message = f"{method}{path}?{query}" if query else f"{method}{path}"
        if body:
            message += body
        return hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()

    # ---- request core ---------------------------------------------------

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None,
                  body: dict[str, Any] | None = None, private: bool = False) -> Any:
        params = dict(params or {})
        headers = {}
        body_str = ""

        if private:
            if not self.api_key or not self.api_secret:
                raise RuntimeError(
                    "This endpoint requires PIONEX_API_KEY / PIONEX_API_SECRET to be set"
                )
            params["timestamp"] = str(int(time.time() * 1000))

        query = self._sorted_query(params)

        if body is not None:
            body_str = json.dumps(body, separators=(",", ":"))

        if private:
            signature = self._sign(method, path, query, body_str)
            headers["PIONEX-KEY"] = self.api_key
            headers["PIONEX-SIGNATURE"] = signature

        url = f"{BASE_URL}{path}"
        if query:
            url += f"?{query}"

        resp = self._session.request(
            method, url, headers=headers,
            data=body_str if body is not None else None,
            timeout=self.timeout,
        )
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise

        if not resp.ok or data.get("result") is False:
            raise PionexAPIError(resp.status_code, data)
        return data

    # ---- public market data --------------------------------------------

    def get_klines(self, symbol: str, interval: str = "5M", limit: int = 100,
                    end_time: int | None = None) -> list[dict]:
        """interval in {1M,5M,15M,30M,60M,4H,8H,12H,1D}, limit 1-500."""
        data = self._request("GET", "/api/v1/market/klines", {
            "symbol": symbol, "interval": interval, "limit": limit, "endTime": end_time,
        })
        return data["data"]["klines"]

    def get_ticker(self, symbol: str | None = None, type_: str = "SPOT") -> Any:
        data = self._request("GET", "/api/v1/market/tickers", {
            "symbol": symbol, "type": type_,
        })
        return data["data"]["tickers"]

    def get_symbol_info(self, symbol: str) -> dict:
        """Public endpoint (no credentials needed) -- exchange-enforced
        precision (decimal places allowed) and minimum order sizes for a
        symbol. Cached per-instance since this is called before every order.
        Without rounding to these limits, an order amount/size carrying
        Python float noise (e.g. 17 significant digits) is rejected outright
        by Pionex with a TRADE_AMOUNT_FILTER_DENIED error."""
        if symbol not in self._symbol_info_cache:
            data = self._request("GET", "/api/v1/common/symbols")
            info = next((s for s in data["data"]["symbols"] if s["symbol"] == symbol), None)
            if info is None:
                raise PionexAPIError(0, f"Unknown symbol {symbol!r} from /api/v1/common/symbols")
            self._symbol_info_cache[symbol] = info
        return self._symbol_info_cache[symbol]

    def get_depth(self, symbol: str, limit: int = 20) -> Any:
        data = self._request("GET", "/api/v1/market/depth", {"symbol": symbol, "limit": limit})
        return data["data"]

    # ---- private: account ------------------------------------------------

    def get_balances(self) -> list[Balance]:
        data = self._request("GET", "/api/v1/account/balances", private=True)
        return [Balance(b["coin"], float(b["free"]), float(b["frozen"]))
                for b in data["data"]["balances"]]

    # ---- private: trading (LIVE ORDERS -- real money) --------------------

    def place_order(self, symbol: str, side: str, order_type: str,
                     size: str | None = None, price: str | None = None,
                     amount: str | None = None, client_order_id: str | None = None,
                     ioc: bool = False) -> Any:
        """side: BUY|SELL, order_type: LIMIT|MARKET.

        LIMIT requires size+price. MARKET buy requires amount (quote currency);
        MARKET sell requires size (base currency).
        This places a REAL order once credentials are configured -- callers in
        this project must go through live.py's explicit confirmation gate.
        """
        body = {"symbol": symbol, "side": side, "type": order_type}
        if size is not None:
            body["size"] = size
        if price is not None:
            body["price"] = price
        if amount is not None:
            body["amount"] = amount
        if client_order_id is not None:
            body["clientOrderId"] = client_order_id
        if ioc:
            body["IOC"] = True
        return self._request("POST", "/api/v1/trade/order", body=body, private=True)

    def cancel_order(self, symbol: str, order_id: int) -> Any:
        return self._request("DELETE", "/api/v1/trade/order",
                              body={"symbol": symbol, "orderId": order_id}, private=True)

    def cancel_all_orders(self, symbol: str) -> Any:
        return self._request("DELETE", "/api/v1/trade/allOrders",
                              body={"symbol": symbol}, private=True)

    def get_order(self, order_id: int) -> Any:
        return self._request("GET", "/api/v1/trade/order", {"orderId": order_id}, private=True)

    def get_open_orders(self, symbol: str) -> Any:
        data = self._request("GET", "/api/v1/trade/openOrders", {"symbol": symbol}, private=True)
        return data["data"]["orders"]

    def get_fills(self, symbol: str, start_time: int | None = None,
                  end_time: int | None = None) -> Any:
        data = self._request("GET", "/api/v1/trade/fills", {
            "symbol": symbol, "startTime": start_time, "endTime": end_time,
        }, private=True)
        return data["data"]["fills"]
