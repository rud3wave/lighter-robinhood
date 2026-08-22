import asyncio
import random
from decimal import Decimal
from typing import Any

import lighter

from .constants import (
    API_KEY_ACTIVATION_WAIT_SECONDS,
    CHAIN_ID,
    DEFAULT_API_KEY_INDEX,
)
from settings import SLIPPAGE

from .api import BookSnapshot, LighterPublicApi, MarketMeta
from .pretty import fmt_number, info, skip, wallet_prefix
from .utils import base_to_int, maker_price_to_int, now_ms, price_to_int, quantize_base
from .wallets import WalletAccount, mask_secret


MAX_CLIENT_ORDER_INDEX = 281_474_976_710_655


def _apply_proxy(client: lighter.SignerClient, proxy_url: str) -> None:
    if not proxy_url:
        return
    client.api_client.configuration.proxy = proxy_url
    client.api_client.rest_client.proxy = proxy_url


class LighterService:
    def __init__(self, wallet: WalletAccount, base_url: str, dry_run: bool):
        self.wallet = wallet
        self.base_url = base_url
        self.dry_run = dry_run
        self.public = LighterPublicApi(base_url, wallet.proxy_url)
        self.client: lighter.SignerClient | None = None
        if not dry_run and wallet.can_trade:
            self.client = lighter.SignerClient(
                url=base_url,
                account_index=wallet.account_index,
                api_private_keys={wallet.api_key_index: wallet.api_private_key},
                chain_id=CHAIN_ID,
            )
            _apply_proxy(self.client, wallet.proxy_url)

    async def close(self) -> None:
        if self.client:
            await self.client.close()

    def label(self) -> str:
        return wallet_prefix(self.wallet.index, mask_secret(self.wallet.address))

    def account_state(self) -> dict:
        if self.wallet.account_index is None:
            matches = self.public.accounts_by_l1_address(self.wallet.address)
            if not matches:
                raise RuntimeError("No Lighter account found for wallet")
            return matches[0]
        return self.public.account(self.wallet.account_index)

    def available_balance(self) -> Decimal:
        return Decimal(str(self.account_state().get("available_balance", "0")))

    async def points(self) -> Decimal:
        if not self.wallet.can_trade or self.client is None:
            raise RuntimeError("торговый доступ не настроен")

        auth, err = self.client.create_auth_token_with_expiry(
            api_key_index=self.wallet.api_key_index
        )
        if err:
            raise RuntimeError(f"не удалось получить Points: {err}")
        try:
            payload = await asyncio.to_thread(
                self.public._get,
                "/api/v1/livePoints/total",
                account_index=self.wallet.account_index,
                auth=auth,
            )
        except Exception as exc:
            raise RuntimeError("не удалось получить Points") from exc

        if int(payload.get("code", 200)) != 200:
            raise RuntimeError(str(payload.get("message") or "не удалось получить Points"))
        value = payload.get("total_live_points")
        if value is None:
            raise RuntimeError("Lighter не вернул Points")
        return Decimal(str(value))

    def position(self, symbol: str) -> dict | None:
        for pos in self.account_state().get("positions", []):
            if str(pos.get("symbol", "")).upper() == symbol.upper() and Decimal(str(pos.get("position", "0"))) != 0:
                return pos
        return None

    async def signed_position(self, symbol: str) -> Decimal:
        pos = await asyncio.to_thread(self.position, symbol)
        if not pos:
            return Decimal(0)
        size = abs(Decimal(str(pos["position"])))
        return size if int(pos["sign"]) > 0 else -size

    @staticmethod
    def quantize_base_amount(meta: MarketMeta, amount: Decimal) -> Decimal:
        return quantize_base(abs(amount), meta.size_decimals)

    async def trade_history(
        self,
        meta: MarketMeta,
        start_timestamp_ms: int = 0,
        end_timestamp_ms: int | None = None,
        max_pages: int = 30,
    ) -> list[Any]:
        if not self.wallet.can_trade or self.client is None:
            raise RuntimeError("Trade history requires a configured API key")

        auth, err = self.client.create_auth_token_with_expiry(
            api_key_index=self.wallet.api_key_index
        )
        if err:
            raise RuntimeError(f"create auth token failed: {err}")

        config = lighter.Configuration(host=self.base_url)
        config.proxy = self.wallet.proxy_url or None
        api_client = lighter.ApiClient(config)
        cursor = None
        out: list[Any] = []
        end_timestamp_ms = end_timestamp_ms or now_ms()
        try:
            api = lighter.OrderApi(api_client)
            for _ in range(max_pages):
                response = await api.trades(
                    sort_by="timestamp",
                    limit=100,
                    authorization=auth,
                    market_id=meta.market_id,
                    account_index=self.wallet.account_index,
                    sort_dir="desc",
                    cursor=cursor,
                )
                rows = list(response.trades or [])
                out.extend(
                    trade
                    for trade in rows
                    if start_timestamp_ms <= int(trade.timestamp) <= end_timestamp_ms
                )
                if not rows:
                    break
                oldest = min(int(trade.timestamp) for trade in rows)
                cursor = response.next_cursor
                if not cursor or oldest <= start_timestamp_ms:
                    break
            return out
        finally:
            await api_client.close()

    async def funding_history(
        self,
        meta: MarketMeta,
        start_timestamp_ms: int,
        end_timestamp_ms: int,
        max_pages: int = 30,
    ) -> list[Any]:
        if not self.wallet.can_trade or self.client is None:
            raise RuntimeError("Funding history requires a configured API key")

        auth, err = self.client.create_auth_token_with_expiry(
            api_key_index=self.wallet.api_key_index
        )
        if err:
            raise RuntimeError(f"create auth token failed: {err}")

        config = lighter.Configuration(host=self.base_url)
        config.proxy = self.wallet.proxy_url or None
        api_client = lighter.ApiClient(config)
        cursor = None
        out: list[Any] = []
        try:
            api = lighter.AccountApi(api_client)
            for _ in range(max_pages):
                response = await api.position_funding(
                    account_index=self.wallet.account_index,
                    limit=100,
                    authorization=auth,
                    market_id=meta.market_id,
                    cursor=cursor,
                    start_timestamp=start_timestamp_ms,
                    end_timestamp=end_timestamp_ms,
                )
                rows = list(response.position_fundings or [])
                out.extend(rows)
                cursor = response.next_cursor
                if not cursor or not rows:
                    break
            return out
        finally:
            await api_client.close()

    async def has_active_order(self, meta: MarketMeta, client_order_index: int | None = None) -> bool:
        if self.dry_run:
            return True
        if not self.wallet.can_trade or self.client is None:
            return False

        api_client = None
        try:
            auth, err = self.client.create_auth_token_with_expiry(
                api_key_index=self.wallet.api_key_index
            )
            if err:
                raise RuntimeError(f"create auth token failed: {err}")
            config = lighter.Configuration(host=self.base_url)
            config.proxy = self.wallet.proxy_url or None
            api_client = lighter.ApiClient(config)
            response = await lighter.OrderApi(api_client).account_active_orders(
                authorization=auth,
                account_index=self.wallet.account_index,
                market_id=meta.market_id,
            )
            orders = list(getattr(response, "orders", []) or [])
            if client_order_index is None:
                return bool(orders)
            return any(int(order.client_order_index) == client_order_index for order in orders)
        finally:
            if api_client:
                await api_client.close()

    async def setup_api_key(self, api_key_index: int = DEFAULT_API_KEY_INDEX) -> bool:
        label = self.label()
        matches = await asyncio.to_thread(self.public.accounts_by_l1_address, self.wallet.address)
        if not matches:
            skip(label, "аккаунт Lighter не найден")
            return False

        account_index = int(matches[0].get("index") or matches[0].get("account_index"))
        if len(matches) > 1:
            account_index = min(int(item.get("index") or item.get("account_index")) for item in matches)

        api_private_key, api_public_key, err = lighter.create_api_key()
        if err:
            raise RuntimeError(f"create_api_key failed for wallet: {err}")

        setup_client = lighter.SignerClient(
            url=self.base_url,
            account_index=account_index,
            api_private_keys={api_key_index: api_private_key},
            chain_id=CHAIN_ID,
        )
        _apply_proxy(setup_client, self.wallet.proxy_url)
        try:
            _, err = await setup_client.change_api_key(
                eth_private_key=self.wallet.private_key,
                new_pubkey=api_public_key,
                api_key_index=api_key_index,
            )
            if err:
                raise RuntimeError(f"change_api_key failed for wallet: {err}")
            if API_KEY_ACTIVATION_WAIT_SECONDS > 0:
                await asyncio.sleep(API_KEY_ACTIVATION_WAIT_SECONDS)
            err = setup_client.check_client()
            if err:
                raise RuntimeError(f"API key check failed for wallet: {err}")
        finally:
            await setup_client.close()

        self.wallet.account_index = account_index
        self.wallet.api_key_index = api_key_index
        self.wallet.api_private_key = api_private_key
        return True

    async def update_leverage(self, meta: MarketMeta, leverage: Decimal | int | float) -> None:
        if not self.wallet.can_trade:
            skip(self.label(), "API key не настроен")
            return
        if self.dry_run:
            return
        assert self.client is not None
        _, resp, err = await self.client.update_leverage(
            meta.market_id,
            self.client.CROSS_MARGIN_MODE,
            leverage,
            api_key_index=self.wallet.api_key_index,
        )
        self._ensure_ok(resp, err, "update leverage")

    async def cancel_all_orders(self, meta: MarketMeta) -> None:
        if not self.wallet.can_trade:
            skip(self.label(), "API key не настроен")
            return
        if self.dry_run:
            return
        assert self.client is not None
        _, resp, err = await self.client.cancel_all_orders(
            self.client.CANCEL_ALL_TIF_IMMEDIATE,
            0,
            cancel_all_market_index=meta.market_id,
            api_key_index=self.wallet.api_key_index,
        )
        self._ensure_ok(resp, err, "cancel all orders")

    async def place_market(
        self,
        meta: MarketMeta,
        book: BookSnapshot,
        side: str,
        amount_usd: Decimal,
        reduce_only: bool = False,
        base_amount_override: Decimal | None = None,
    ) -> int:
        is_buy = side == "long"
        if base_amount_override is None:
            raw_base = amount_usd / (book.best_ask if is_buy else book.best_bid)
            base_amount = max(raw_base, meta.min_base_amount)
            if base_amount * book.mid < meta.min_quote_amount:
                base_amount = meta.min_quote_amount / book.mid
        else:
            base_amount = abs(base_amount_override)

        base_i = base_to_int(base_amount, meta.size_decimals)
        submitted_base = Decimal(base_i) / (Decimal(10) ** meta.size_decimals)
        slip = Decimal(str(SLIPPAGE)) / Decimal(100)
        worst_price = book.best_ask * (Decimal(1) + slip) if is_buy else book.best_bid * (Decimal(1) - slip)
        price_i = price_to_int(worst_price, meta.price_decimals, is_buy)
        submitted_price = Decimal(price_i) / (Decimal(10) ** meta.price_decimals)
        client_order_index = (now_ms() * 1000 + random.randrange(1000)) % MAX_CLIENT_ORDER_INDEX
        action = "LONG" if is_buy else "SHORT"
        approx_usd = submitted_base * book.mid
        base_text = fmt_number(submitted_base)
        price_text = fmt_number(submitted_price, decimal_separator=",")
        order_label = f"{mask_secret(self.wallet.address)} {action}"
        info(
            order_label,
            f"${fmt_number(approx_usd)} | {base_text} {meta.symbol} | "
            f"ENTRY {price_text}",
        )
        if not self.wallet.can_trade:
            skip(self.label(), "API key не настроен")
            return client_order_index
        if self.dry_run:
            return client_order_index
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
            api_key_index=self.wallet.api_key_index,
        )
        self._ensure_ok(resp, err, "create market order")
        return client_order_index

    async def place_post_only(
        self,
        meta: MarketMeta,
        book: BookSnapshot,
        side: str,
        base_amount: Decimal,
        reduce_only: bool = False,
    ) -> tuple[int, Decimal]:
        is_buy = side == "long"
        base_amount = self.quantize_base_amount(meta, base_amount)
        if base_amount <= 0:
            raise RuntimeError("Post-only order amount rounded to zero")

        tick = Decimal(1) / (Decimal(10) ** meta.price_decimals)
        can_improve = book.best_ask - book.best_bid > tick
        if is_buy:
            order_price = book.best_ask - tick if can_improve else book.best_bid
            maker_reference = book.best_ask
        else:
            order_price = book.best_bid + tick if can_improve else book.best_ask
            maker_reference = book.best_bid
        base_i = base_to_int(base_amount, meta.size_decimals)
        price_i = maker_price_to_int(order_price, meta.price_decimals, is_buy)
        price = Decimal(price_i) / (Decimal(10) ** meta.price_decimals)
        client_order_index = (now_ms() * 1000 + random.randrange(1000)) % MAX_CLIENT_ORDER_INDEX
        action = "LONG" if is_buy else "SHORT"
        notional = base_amount * price
        base_text = fmt_number(base_amount)
        price_text = fmt_number(price, decimal_separator=",")
        info(
            f"{self.label()} {action}",
            f"${fmt_number(notional)} | {base_text} {meta.symbol} | "
            f"ENTRY {price_text} | LIMIT",
        )
        if not self.wallet.can_trade:
            skip(self.label(), "API key не настроен")
            return client_order_index, maker_reference
        if self.dry_run:
            return client_order_index, maker_reference

        assert self.client is not None
        _, resp, err = await self.client.create_order(
            meta.market_id,
            client_order_index,
            base_i,
            price_i,
            not is_buy,
            self.client.ORDER_TYPE_LIMIT,
            self.client.ORDER_TIME_IN_FORCE_POST_ONLY,
            reduce_only,
            self.client.NIL_TRIGGER_PRICE,
            self.client.DEFAULT_28_DAY_ORDER_EXPIRY,
            self_trade_behavior_mode=self.client.SELF_TRADE_BEHAVIOR_EXPIRE_BOTH,
            self_trade_equality_mode=self.client.SELF_TRADE_EQUALITY_MASTER_ACCOUNT_INDEX,
            api_key_index=self.wallet.api_key_index,
        )
        self._ensure_ok(resp, err, "create post-only order")
        return client_order_index, maker_reference

    async def close_position(self, meta: MarketMeta) -> None:
        pos = await asyncio.to_thread(self.position, meta.symbol)
        if not pos:
            skip(self.label(), f"позиция {meta.symbol} отсутствует")
            return
        sign = int(pos["sign"])
        size = abs(Decimal(str(pos["position"])))
        side = "short" if sign > 0 else "long"
        book = await asyncio.to_thread(self.public.book, meta.market_id)
        amount_usd = size * book.mid
        await self.cancel_all_orders(meta)
        await self.place_market(meta, book, side, amount_usd, reduce_only=True, base_amount_override=size)

    @staticmethod
    def proxy_text(wallet: WalletAccount) -> str:
        return " proxy=pool" if wallet.proxy_url else " no-proxy"

    @staticmethod
    def _ensure_ok(resp, err: str | None, action: str) -> None:
        if err:
            raise RuntimeError(f"{action} failed: {err}")
        code = getattr(resp, "code", None)
        if code not in (None, 200):
            raise RuntimeError(f"{action} returned code={code}: {resp}")
