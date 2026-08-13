from decimal import Decimal

import lighter

from settings import SLIPPAGE

from .accounts import AccountConfig
from .api import BookSnapshot, LighterPublicApi, MarketMeta
from .utils import base_to_int, now_ms, price_to_int, short_id


class LighterService:
    def __init__(self, config: AccountConfig, base_url: str, dry_run: bool):
        self.config = config
        self.base_url = base_url
        self.dry_run = dry_run
        self.public = LighterPublicApi(base_url)
        self.client = None
        if not dry_run:
            self.client = lighter.SignerClient(
                url=base_url,
                account_index=config.account_index,
                api_private_keys={config.api_key_index: config.api_private_key},
            )

    async def close(self) -> None:
        if self.client:
            await self.client.close()

    def label(self) -> str:
        return f"{self.config.name}#{short_id(self.config.account_index)}"

    def account_state(self) -> dict:
        return self.public.account(self.config.account_index)

    def available_balance(self) -> Decimal:
        return Decimal(str(self.account_state().get("available_balance", "0")))

    def position(self, symbol: str) -> dict | None:
        for pos in self.account_state().get("positions", []):
            if str(pos.get("symbol", "")).upper() == symbol.upper() and Decimal(str(pos.get("position", "0"))) != 0:
                return pos
        return None

    async def update_leverage(self, meta: MarketMeta, leverage: int) -> None:
        print(f"  {self.label()} leverage {meta.symbol}: {leverage}x")
        if self.dry_run:
            return
        assert self.client is not None
        _, resp, err = await self.client.update_leverage(
            meta.market_id,
            self.client.CROSS_MARGIN_MODE,
            leverage,
            api_key_index=self.config.api_key_index,
        )
        self._ensure_ok(resp, err, "update leverage")

    async def cancel_all_orders(self, meta: MarketMeta) -> None:
        print(f"  {self.label()} cancel all {meta.symbol}")
        if self.dry_run:
            return
        assert self.client is not None
        _, resp, err = await self.client.cancel_all_orders(
            self.client.CANCEL_ALL_TIF_IMMEDIATE,
            now_ms(),
            cancel_all_market_index=meta.market_id,
            api_key_index=self.config.api_key_index,
        )
        self._ensure_ok(resp, err, "cancel all orders")

    async def place_market(
        self,
        meta: MarketMeta,
        book: BookSnapshot,
        side: str,
        amount_usd: Decimal,
        reduce_only: bool = False,
    ) -> None:
        is_buy = side == "long"
        raw_base = amount_usd / (book.best_ask if is_buy else book.best_bid)
        base_amount = max(raw_base, meta.min_base_amount)
        if base_amount * book.mid < meta.min_quote_amount:
            base_amount = meta.min_quote_amount / book.mid

        base_i = base_to_int(base_amount, meta.size_decimals)
        slip = Decimal(str(SLIPPAGE)) / Decimal(100)
        worst_price = book.best_ask * (Decimal(1) + slip) if is_buy else book.best_bid * (Decimal(1) - slip)
        price_i = price_to_int(worst_price, meta.price_decimals, is_buy)
        client_order_index = now_ms() % 281474976710655
        action = "BUY/LONG" if is_buy else "SELL/SHORT"
        ro = " reduce-only" if reduce_only else ""
        print(
            f"  {self.label()} {action}{ro}: ${amount_usd:.2f}, "
            f"base={base_amount:.8f}, price_i={price_i}, order={client_order_index}"
        )
        if self.dry_run:
            return
        assert self.client is not None
        _, resp, err = await self.client.create_order(
            meta.market_id,
            client_order_index,
            base_i,
            price_i,
            not is_buy,
            self.client.ORDER_TYPE_MARKET,
            self.client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            reduce_only,
            self.client.NIL_TRIGGER_PRICE,
            self.client.DEFAULT_IOC_EXPIRY,
            self_trade_behavior_mode=self.client.SELF_TRADE_BEHAVIOR_EXPIRE_BOTH,
            self_trade_equality_mode=self.client.SELF_TRADE_EQUALITY_MASTER_ACCOUNT_INDEX,
            api_key_index=self.config.api_key_index,
        )
        self._ensure_ok(resp, err, "create market order")

    async def close_position(self, meta: MarketMeta) -> None:
        pos = self.position(meta.symbol)
        if not pos:
            print(f"  {self.label()} no {meta.symbol} position")
            return
        sign = int(pos["sign"])
        size = abs(Decimal(str(pos["position"])))
        side = "short" if sign > 0 else "long"
        book = self.public.book(meta.market_id)
        amount_usd = size * book.mid
        await self.cancel_all_orders(meta)
        await self.place_market(meta, book, side, amount_usd, reduce_only=True)

    @staticmethod
    def _ensure_ok(resp, err: str | None, action: str) -> None:
        if err:
            raise RuntimeError(f"{action} failed: {err}")
        code = getattr(resp, "code", None)
        if code not in (None, 200):
            raise RuntimeError(f"{action} returned code={code}: {resp}")
