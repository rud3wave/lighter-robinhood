import json
import urllib.parse
import urllib.request
from urllib.error import HTTPError
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class MarketMeta:
    symbol: str
    market_id: int
    size_decimals: int
    price_decimals: int
    min_base_amount: Decimal
    min_quote_amount: Decimal
    max_leverage: Decimal


@dataclass(frozen=True)
class BookSnapshot:
    best_bid: Decimal
    best_ask: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.best_bid + self.best_ask) / Decimal(2)

    @property
    def spread_percent(self) -> Decimal:
        return (self.best_ask - self.best_bid) / self.mid * Decimal(100)


class LighterPublicApi:
    def __init__(self, base_url: str, proxy_url: str = ""):
        self.base_url = base_url.rstrip("/")
        self.proxy_url = proxy_url

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        req = urllib.request.Request(url, headers={"accept": "application/json"})
        if self.proxy_url:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": self.proxy_url, "https": self.proxy_url})
            )
            response = opener.open(req, timeout=30)
        else:
            response = urllib.request.urlopen(req, timeout=30)
        with response as resp:
            return json.loads(resp.read().decode("utf-8"))

    def market_meta(self, symbol: str) -> MarketMeta:
        payload = self._get("/api/v1/orderBookDetails", filter="perp")
        for item in payload.get("order_book_details", []):
            if str(item.get("symbol", "")).upper() == symbol.upper():
                min_imf = Decimal(str(item["min_initial_margin_fraction"]))
                return MarketMeta(
                    symbol=str(item["symbol"]),
                    market_id=int(item["market_id"]),
                    size_decimals=int(item["supported_size_decimals"]),
                    price_decimals=int(item["supported_price_decimals"]),
                    min_base_amount=Decimal(str(item["min_base_amount"])),
                    min_quote_amount=Decimal(str(item["min_quote_amount"])),
                    max_leverage=(Decimal(10_000) / min_imf),
                )
        raise RuntimeError(f"Market {symbol} not found on {self.base_url}")

    def book(self, market_id: int) -> BookSnapshot:
        payload = self._get("/api/v1/orderBookOrders", market_id=market_id, limit=5)
        bids = payload.get("bids", [])
        asks = payload.get("asks", [])
        if not bids or not asks:
            raise RuntimeError(f"Order book {market_id} has no bid/ask")
        return BookSnapshot(
            best_bid=Decimal(str(bids[0]["price"])),
            best_ask=Decimal(str(asks[0]["price"])),
        )

    def account(self, account_index: int) -> dict[str, Any]:
        payload = self._get("/api/v1/account", by="index", value=str(account_index), active_only="false")
        accounts = payload.get("accounts", [])
        if not accounts:
            raise RuntimeError(f"Lighter account {account_index} not found")
        return accounts[0]

    def accounts_by_l1_address(self, l1_address: str) -> list[dict[str, Any]]:
        try:
            payload = self._get("/api/v1/accountsByL1Address", l1_address=l1_address)
        except HTTPError as exc:
            if exc.code == 400:
                return []
            raise
        return list(payload.get("sub_accounts") or payload.get("accounts") or [])
