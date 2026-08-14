import asyncio
import random
from decimal import Decimal

from settings import (
    API_BASE_URL,
    DEPOSIT_ALL,
    DEPOSIT_AMOUNT,
    DEPOSIT_TOKEN_SYMBOL,
    DEFAULT_API_KEY_INDEX,
    DELAY_BETWEEN_TRADES,
    DELAY_BETWEEN_WALLETS,
    EXECUTION_MODE,
    GROUP_CONFIGS,
    HOLD_MINUTES,
    MAX_SPREAD,
    POST_DEPOSIT_ACCOUNT_POLL_ATTEMPTS,
    POST_DEPOSIT_ACCOUNT_POLL_SECONDS,
    POSITION_PERCENT,
    POLL_INTERVAL_SEC,
    RETRY,
    SPREAD_POLL_INTERVAL_SECONDS,
    SPREAD_LOG_INTERVAL_SECONDS,
    SPREAD_WAIT_TIMEOUT_SECONDS,
    SYMBOL,
    TOKEN_LEVERAGE,
    TOKENS_TO_TRADE,
    TRADES_COUNT,
)

from .allocation import Allocation, calculate_allocation
from .api import BookSnapshot, LighterPublicApi
from .deposit import deposit_token, pick_deposit_amount
from .pretty import error, exception_summary, info, ok, section, skip, step, warn, wallet_prefix
from .paired_execution import (
    PairedTarget,
    allocate_targets,
    close_leader_follower,
    execute_paired,
    flatten_positions,
)
from .referral import use_referral
from .runtime_control import is_trading_halted
from .service import LighterService
from .telegram import send_tg
from .trade_report import TradeMetrics, discover_open_timestamp, summarize_trades
from .utils import now_ms, pick_range
from .wallets import WalletAccount, init_wallets, mask_secret, save_wallets


def _wallet_log_label(wallet: WalletAccount) -> str:
    return wallet_prefix(wallet.index, mask_secret(wallet.address))


def _tg_service_label(service: LighterService) -> str:
    return mask_secret(service.wallet.address)


class Controller:
    def __init__(self):
        self.public = LighterPublicApi(API_BASE_URL)
        self.metas = {
            symbol: self.public.market_meta(symbol)
            for symbol in dict.fromkeys(TOKENS_TO_TRADE)
        }

    def _meta(self, symbol: str):
        try:
            return self.metas[symbol]
        except KeyError as exc:
            raise RuntimeError(f"Market {symbol} is not configured") from exc

    def market_info(self) -> None:
        meta = self._meta(TOKENS_TO_TRADE[0])
        book = self.public.book(meta.market_id)
        section("Market info", f"{meta.symbol} id={meta.market_id}")
        info("Best bid / ask", f"{book.best_bid} / {book.best_ask}")
        info("Spread", f"{book.spread_percent:.4f}%")
        info(
            "Limits",
            f"min base={meta.min_base_amount}, min quote=${meta.min_quote_amount}, "
            f"size_dec={meta.size_decimals}, price_dec={meta.price_decimals}, max lev={meta.max_leverage}x"
        )

    def setup_status(self) -> None:
        try:
            wallets = self._load_wallets()
        except Exception as exc:
            section("Setup")
            error("Wallets", "0")
            error("Input error", exception_summary(exc))
            info("Next step", "Fill input_data/privatekeys.txt with EVM private keys")
            return
        tradable = [wallet for wallet in wallets if wallet.can_trade]
        resolved = [wallet for wallet in wallets if wallet.account_index is not None]
        section("Setup")
        info("Wallets", len(wallets))
        info("Resolved Lighter accounts", len(resolved))
        info("Tradable API keys", len(tradable))
        if not wallets:
            info("Next step", "Fill input_data/privatekeys.txt with EVM private keys")
        elif not tradable:
            info("Next step", "Run mode 4 to register/deposit or mode 1 in live mode")

    async def setup_api_keys(self) -> None:
        wallets = self._load_wallets()
        if not wallets:
            raise RuntimeError("No wallets loaded from input_data/privatekeys.txt")
        section("API-key setup", f"parallel={len(wallets)} | index={DEFAULT_API_KEY_INDEX}")
        warn("Existing key at this index will be replaced")

        async def setup_one(wallet: WalletAccount) -> bool:
            service = LighterService(wallet, API_BASE_URL, dry_run=True)
            try:
                return await service.setup_api_key(DEFAULT_API_KEY_INDEX)
            except Exception as exc:
                error(_wallet_log_label(wallet), exception_summary(exc))
                return False
            finally:
                await service.close()

        changed = any(await asyncio.gather(*(setup_one(wallet) for wallet in wallets)))
        if changed:
            save_wallets(wallets)
            await send_tg(f"Lighter {SYMBOL}: API-key setup finished for {len(wallets)} wallet(s)")

    async def resolve_accounts(self, wallets: list[WalletAccount] | None = None) -> list[WalletAccount]:
        wallets = wallets or self._load_wallets()
        async def resolve_one(wallet: WalletAccount) -> bool:
            try:
                changed = await self._resolve_wallet_account(wallet)
                if wallet.account_index is None:
                    skip(_wallet_log_label(wallet), "no Lighter account")
                return changed
            except Exception as exc:
                error(_wallet_log_label(wallet), exception_summary(exc))
                return False

        changed = any(await asyncio.gather(*(resolve_one(wallet) for wallet in wallets)))
        if changed:
            save_wallets(wallets)
        return wallets

    async def deposit_from_wallets(self) -> None:
        wallets = self._load_wallets()
        if not wallets:
            raise RuntimeError("No wallets loaded from input_data/privatekeys.txt")

        section(
            f"Referral + {DEPOSIT_TOKEN_SYMBOL} deposit",
            f"parallel={len(wallets)} | amount={'ALL' if DEPOSIT_ALL else DEPOSIT_AMOUNT}",
        )

        async def deposit_one(wallet: WalletAccount) -> tuple[str | None, bool, bool]:
            try:
                wallet_changed = await self._resolve_wallet_account(wallet)
                had_account = wallet.account_index is not None
                if had_account:
                    try:
                        wallet_changed = (
                            await self._ensure_api_key(wallet)
                            or wallet_changed
                        )
                        await use_referral(wallet)
                    except Exception as exc:
                        warn(_wallet_log_label(wallet), f"referral setup failed; deposit continues: {exception_summary(exc)}")

                amount = None if DEPOSIT_ALL else pick_deposit_amount(DEPOSIT_AMOUNT)
                tx_hash = await deposit_token(wallet, amount)
                return tx_hash, bool(tx_hash and not had_account), wallet_changed
            except Exception as exc:
                error(_wallet_log_label(wallet), f"deposit error: {exception_summary(exc)}")
                return None, False, False

        deposit_results = await asyncio.gather(*(deposit_one(wallet) for wallet in wallets))
        tx_hashes = [tx_hash for tx_hash, _, _ in deposit_results if tx_hash]
        pending_registration = [
            wallet
            for wallet, (_, pending, _) in zip(wallets, deposit_results)
            if pending
        ]
        changed = any(wallet_changed for _, _, wallet_changed in deposit_results)

        async def finish_registration(wallet: WalletAccount) -> bool:
            try:
                resolved = await self._wait_for_account(wallet)
                if resolved:
                    changed_now = await self._ensure_api_key(wallet)
                    await use_referral(wallet)
                    return changed_now or resolved
                return False
            except Exception as exc:
                warn(_wallet_log_label(wallet), f"post-deposit setup pending: {exception_summary(exc)}")
                return False

        if pending_registration:
            registration_results = await asyncio.gather(
                *(finish_registration(wallet) for wallet in pending_registration)
            )
            changed = any(registration_results) or changed
        if changed:
            save_wallets(wallets)
        ok("Deposit batch finished", f"confirmed txs={len(tx_hashes)}/{len(wallets)}")
        lines = [f"💰 USDG DEPOSIT | Lighter | {len(wallets)} wallet(s)", ""]
        for wallet, (tx_hash, pending, _) in zip(wallets, deposit_results):
            if tx_hash:
                state = "account pending" if pending else "confirmed"
                lines.append(
                    f"✅ {mask_secret(wallet.address)} | "
                    f"{state} | tx {mask_secret(tx_hash)}"
                )
            else:
                lines.append(f"⚠️ {mask_secret(wallet.address)} | no deposit")
        lines.extend(["", f"Confirmed transactions: {len(tx_hashes)}/{len(wallets)}"])
        await send_tg("\n".join(lines))

    async def _services(self, require_trading: bool = False) -> list[LighterService]:
        wallets = self._load_wallets()
        wallets = await self._prepare_wallets(wallets, require_trading=require_trading)
        if require_trading:
            wallets = [
                wallet
                for wallet in wallets
                if wallet.can_trade
            ]
            if not wallets:
                raise RuntimeError("No ready wallets. Check privatekeys.txt and deposit/create a Lighter account first.")
        if not wallets:
            raise RuntimeError("No wallets loaded from input_data/privatekeys.txt")
        return [LighterService(wallet, API_BASE_URL, dry_run=False) for wallet in wallets]

    def _load_wallets(self) -> list[WalletAccount]:
        return init_wallets()

    async def _prepare_wallets(self, wallets: list[WalletAccount], require_trading: bool) -> list[WalletAccount]:
        async def resolve_one(wallet: WalletAccount) -> bool:
            try:
                return await self._resolve_wallet_account(wallet)
            except Exception as exc:
                error(_wallet_log_label(wallet), f"account lookup failed: {exception_summary(exc)}")
                return False

        resolved = await asyncio.gather(*(resolve_one(wallet) for wallet in wallets))
        changed = any(resolved)

        if require_trading:
            async def ensure_one(wallet: WalletAccount) -> bool:
                try:
                    return await self._ensure_api_key(wallet)
                except Exception as exc:
                    error(_wallet_log_label(wallet), f"API-key setup failed: {exception_summary(exc)}")
                    return False

            keys = await asyncio.gather(*(ensure_one(wallet) for wallet in wallets))
            changed = any(keys) or changed

        if changed:
            save_wallets(wallets)
        return wallets

    async def _resolve_wallet_account(self, wallet: WalletAccount) -> bool:
        if wallet.account_index is not None:
            return False
        service = LighterService(wallet, API_BASE_URL, dry_run=True)
        try:
            matches = await asyncio.to_thread(service.public.accounts_by_l1_address, wallet.address)
            if not matches:
                return False
            wallet.account_index = min(int(item.get("index") or item.get("account_index")) for item in matches)
            ok(_wallet_log_label(wallet), "account resolved")
            return True
        finally:
            await service.close()

    async def _ensure_api_key(self, wallet: WalletAccount) -> bool:
        if wallet.can_trade:
            return False
        if wallet.account_index is None:
            skip(_wallet_log_label(wallet), "no Lighter account, skip API-key setup")
            return False
        service = LighterService(wallet, API_BASE_URL, dry_run=True)
        try:
            ok = await service.setup_api_key(DEFAULT_API_KEY_INDEX)
        finally:
            await service.close()
        return ok

    async def _wait_for_account(self, wallet: WalletAccount) -> bool:
        if POST_DEPOSIT_ACCOUNT_POLL_ATTEMPTS <= 0:
            return False
        step(
            _wallet_log_label(wallet),
            f"waiting for Lighter account | {POST_DEPOSIT_ACCOUNT_POLL_ATTEMPTS}x{POST_DEPOSIT_ACCOUNT_POLL_SECONDS}s"
        )
        for attempt in range(POST_DEPOSIT_ACCOUNT_POLL_ATTEMPTS):
            if attempt > 0:
                await asyncio.sleep(POST_DEPOSIT_ACCOUNT_POLL_SECONDS)
            if await self._resolve_wallet_account(wallet):
                return True
        warn(_wallet_log_label(wallet), "account not created yet; run mode 4 again later")
        return False

    async def balances(self) -> None:
        services = await self._services(require_trading=False)
        try:
            section("Balances", f"parallel={len(services)}")

            async def read_balance(svc: LighterService) -> tuple[Decimal, str]:
                try:
                    state = await asyncio.to_thread(svc.account_state)
                    available = Decimal(str(state.get("available_balance", "0")))
                    collateral = Decimal(str(state.get("collateral", "0")))
                    configured = {symbol.upper() for symbol in TOKENS_TO_TRADE}
                    positions = [
                        item
                        for item in state.get("positions", [])
                        if str(item.get("symbol", "")).upper() in configured
                        and Decimal(str(item.get("position", "0"))) != 0
                    ]
                    pos_text = "flat" if not positions else ", ".join(
                        f"{item['position']} {item['symbol']} sign={item['sign']}"
                        for item in positions
                    )
                    info(
                        svc.label(),
                        f"available=${available:.4f} | collateral=${collateral:.4f} | position={pos_text}"
                    )
                    try:
                        multiplier = await svc.reward_point_multiplier()
                        if multiplier is not None:
                            info(svc.label(), f"API reward_point_multiplier={multiplier}x")
                    except Exception as exc:
                        warn(svc.label(), f"reward multiplier unavailable: {exception_summary(exc)}")
                    line = f"✅ {_tg_service_label(svc)} | ${available:.4f} | {pos_text}"
                    return available, line
                except Exception as exc:
                    error(svc.label(), exception_summary(exc))
                    return Decimal(0), f"❌ {_tg_service_label(svc)} | {exception_summary(exc)}"

            results = await asyncio.gather(*(read_balance(svc) for svc in services))
            total = sum((balance for balance, _ in results), Decimal(0))
            ok("Total available", f"${total:.4f}")
            lines = ["💰 CHECKED BALANCES | Lighter", ""]
            lines.extend(line for _, line in results)
            lines.extend(["", f"💎 Total: ${total:.4f} across {len(services)} wallet(s)"])
            await send_tg("\n".join(lines))
        finally:
            await asyncio.gather(*(svc.close() for svc in services), return_exceptions=True)

    async def cancel_all(self) -> None:
        services = await self._services(require_trading=True)
        try:
            for symbol in TOKENS_TO_TRADE:
                meta = self._meta(symbol)
                await asyncio.gather(*(svc.cancel_all_orders(meta) for svc in services))
            await send_tg(
                f"Lighter {', '.join(TOKENS_TO_TRADE)}: cancel-all sent for {len(services)} wallet(s)"
            )
        finally:
            await asyncio.gather(*(svc.close() for svc in services), return_exceptions=True)

    async def _discover_cycle_start(
        self,
        services: list[LighterService],
        meta,
    ) -> int:
        histories = await asyncio.gather(
            *(service.trade_history(meta) for service in services),
            return_exceptions=True,
        )
        starts = []
        for service, history in zip(services, histories):
            if isinstance(history, BaseException):
                raise RuntimeError(
                    f"{service.label()} trade history failed: {exception_summary(history)}"
                )
            starts.append(
                discover_open_timestamp(history, int(service.wallet.account_index))
            )
        if not starts:
            raise RuntimeError("no positioned wallets for cycle discovery")
        return min(starts) - 1000

    async def _collect_trade_metrics(
        self,
        services: list[LighterService],
        meta,
        start_timestamp_ms: int,
        close_started_ms: int,
    ) -> dict[LighterService, TradeMetrics]:
        async def collect_one(service: LighterService) -> TradeMetrics:
            trades = []
            end_timestamp_ms = now_ms()
            for attempt in range(1, 7):
                if attempt > 1:
                    await asyncio.sleep(2)
                end_timestamp_ms = now_ms()
                trades = await service.trade_history(
                    meta,
                    start_timestamp_ms=start_timestamp_ms,
                    end_timestamp_ms=end_timestamp_ms,
                )
                newest = max((int(trade.timestamp) for trade in trades), default=0)
                if newest >= close_started_ms:
                    break
            funding = await service.funding_history(
                meta,
                start_timestamp_ms=start_timestamp_ms,
                end_timestamp_ms=end_timestamp_ms,
            )
            return summarize_trades(
                trades,
                int(service.wallet.account_index),
                funding,
            )

        results = await asyncio.gather(
            *(collect_one(service) for service in services),
            return_exceptions=True,
        )
        metrics: dict[LighterService, TradeMetrics] = {}
        for service, result in zip(services, results):
            if isinstance(result, BaseException):
                warn(service.label(), f"trade report failed: {exception_summary(result)}")
            else:
                metrics[service] = result
        return metrics

    async def close_positions(self) -> None:
        services = await self._services(require_trading=True)
        try:
            section("Close all", "leader LIMIT -> follower MARKET -> residual MARKET")
            close_started_ms = now_ms()
            states_before = await asyncio.gather(
                *(asyncio.to_thread(service.account_state) for service in services),
                return_exceptions=True,
            )
            fallback_close_volume = Decimal(0)
            positioned: set[LighterService] = set()
            report_specs: list[tuple[list[LighterService], object, int, bool]] = []

            for symbol in TOKENS_TO_TRADE:
                meta = self._meta(symbol)
                await asyncio.gather(
                    *(service.cancel_all_orders(meta) for service in services),
                    return_exceptions=True,
                )
                positions = await asyncio.gather(
                    *(service.signed_position(symbol) for service in services),
                    return_exceptions=True,
                )
                read_errors = [
                    (service, position)
                    for service, position in zip(services, positions)
                    if isinstance(position, BaseException)
                ]
                for service, position in read_errors:
                    warn(
                        service.label(),
                        f"position read failed; forcing residual close: {exception_summary(position)}",
                    )
                entries = [
                    (service, position)
                    for service, position in zip(services, positions)
                    if not isinstance(position, BaseException) and position != 0
                ]
                if not entries:
                    if read_errors:
                        await flatten_positions(services, meta)
                    else:
                        skip(symbol, "no open positions")
                    continue

                positioned.update(service for service, _ in entries)
                book = await asyncio.to_thread(self.public.book, meta.market_id)
                fallback_close_volume += sum(
                    (abs(position) * book.mid for _, position in entries),
                    Decimal(0),
                )
                positioned_services = [service for service, _ in entries]
                full_cycle = True
                try:
                    report_start = await self._discover_cycle_start(positioned_services, meta)
                    info(symbol, "full-cycle trade history located")
                except Exception as exc:
                    full_cycle = False
                    report_start = close_started_ms
                    warn(symbol, f"using close-only trade report: {exception_summary(exc)}")
                report_specs.append((positioned_services, meta, report_start, full_cycle))
                biggest = max(entries, key=lambda item: abs(item[1]))
                leader_is_long = biggest[1] > 0
                leader_services = [
                    service for service, position in entries if (position > 0) == leader_is_long
                ]
                follower_services = [
                    service for service, position in entries if (position > 0) != leader_is_long
                ]
                info(
                    f"{symbol} paired close",
                    f"leader positions={len(leader_services)} | followers={len(follower_services)}",
                )
                await close_leader_follower(
                    leader_services,
                    follower_services,
                    meta,
                )
                await flatten_positions(services, meta)

            states_after = await asyncio.gather(
                *(asyncio.to_thread(service.account_state) for service in services),
                return_exceptions=True,
            )
            wallet_metrics: dict[LighterService, TradeMetrics] = {}
            full_cycle_report = bool(report_specs) and all(spec[3] for spec in report_specs)
            for positioned_services, meta, report_start, _ in report_specs:
                collected = await self._collect_trade_metrics(
                    positioned_services,
                    meta,
                    report_start,
                    close_started_ms,
                )
                for service, metrics in collected.items():
                    wallet_metrics[service] = wallet_metrics.get(service, TradeMetrics()) + metrics
            full_cycle_report = full_cycle_report and all(
                service in wallet_metrics for service in positioned
            )

            lines = ["📂 POSITIONS CLOSED | Force close", ""]
            total_pnl = Decimal(0)
            actual_volume = Decimal(0)
            total_realized = Decimal(0)
            total_funding = Decimal(0)
            for service, before, after in zip(services, states_before, states_after):
                if isinstance(after, BaseException):
                    lines.append(f"❌ {_tg_service_label(service)} | balance unavailable")
                    continue
                balance_after = Decimal(str(after.get("available_balance", "0")))
                metrics = wallet_metrics.get(service)
                if metrics is not None and metrics.fills > 0:
                    pnl = metrics.net_pnl
                    volume = metrics.volume
                    total_realized += metrics.realized_pnl
                    total_funding += metrics.funding
                elif not isinstance(before, BaseException):
                    pnl = Decimal(str(after.get("collateral", "0"))) - Decimal(
                        str(before.get("collateral", "0"))
                    )
                    volume = Decimal(0)
                else:
                    lines.append(f"❌ {_tg_service_label(service)} | PnL unavailable")
                    continue
                total_pnl += pnl
                actual_volume += volume
                state = "closed" if service in positioned else "no open positions"
                lines.append(
                    f"✅ {_tg_service_label(service)} | {state} | "
                    f"PnL: {pnl:+.4f}$ | Bal: ${balance_after:.4f}"
                )
            report_volume = actual_volume or fallback_close_volume
            cost_per_100k = (
                -total_pnl / report_volume * Decimal(100_000)
                if report_volume > 0
                else Decimal(0)
            )
            scope = "Full-cycle" if full_cycle_report and actual_volume > 0 else "Close"
            lines.extend(
                [
                    "",
                    f"📊 {scope} PnL: {total_pnl:+.4f}$",
                    f"Realized from fills: {total_realized:+.4f}$",
                    f"Funding: {total_funding:+.4f}$",
                    f"💰 {scope} volume: ${report_volume:.2f}",
                    f"Cost per 100k: ${cost_per_100k:.3f}",
                ]
            )
            ok("Close all complete", f"wallets={len(services)}")
            await send_tg("\n".join(lines))
        finally:
            await asyncio.gather(*(svc.close() for svc in services), return_exceptions=True)

    async def _wait_for_spread(self, meta) -> BookSnapshot:
        loop = asyncio.get_running_loop()
        started = loop.time()
        next_log = started
        while True:
            book = await asyncio.to_thread(self.public.book, meta.market_id)
            if book.spread_percent <= Decimal(str(MAX_SPREAD)):
                ok(
                    "Spread ready",
                    f"{book.spread_percent:.4f}% <= {Decimal(str(MAX_SPREAD)):.4f}%",
                )
                return book
            now = loop.time()
            if now - started >= SPREAD_WAIT_TIMEOUT_SECONDS:
                raise RuntimeError(
                    f"Spread stayed at {book.spread_percent:.4f}% above MAX_SPREAD={MAX_SPREAD}% "
                    f"for {SPREAD_WAIT_TIMEOUT_SECONDS}s"
                )
            if now >= next_log:
                step(
                    "Waiting for spread",
                    f"current={book.spread_percent:.4f}% | max={Decimal(str(MAX_SPREAD)):.4f}%",
                )
                next_log = now + SPREAD_LOG_INTERVAL_SECONDS
            await asyncio.sleep(SPREAD_POLL_INTERVAL_SECONDS)

    async def _verify_flat(self, services: list[LighterService], symbol: str) -> None:
        positions = await asyncio.gather(*(service.signed_position(symbol) for service in services))
        non_flat = [
            service.label()
            for service, position in zip(services, positions)
            if position != 0
        ]
        if non_flat:
            raise RuntimeError(
                f"Selected group has {len(non_flat)} existing {symbol} position(s); run mode 2 first"
            )

    def _build_targets(
        self,
        meta,
        allocations: list[Allocation],
        leader_side: str,
        book: BookSnapshot,
        validate_maker: bool = True,
    ) -> tuple[list[PairedTarget], list[PairedTarget]]:
        leader_allocations = [item for item in allocations if item.side == leader_side]
        follower_allocations = [item for item in allocations if item.side != leader_side]
        leader_targets = [
            PairedTarget(
                item.service,
                item.service.quantize_base_amount(meta, item.notional / book.mid),
            )
            for item in leader_allocations
        ]
        if any(target.target_base <= 0 for target in leader_targets):
            raise RuntimeError("A leader target rounded to zero")
        for target in leader_targets if validate_maker else []:
            if target.target_base < meta.min_base_amount:
                raise RuntimeError(
                    f"Leader target {target.target_base} is below min base {meta.min_base_amount}"
                )
            if target.target_base * book.mid < meta.min_quote_amount:
                raise RuntimeError(
                    f"Leader target quote is below ${meta.min_quote_amount}"
                )

        leader_total = sum((target.target_base for target in leader_targets), Decimal(0))
        follower_targets = allocate_targets(
            leader_total,
            [(item.service, item.notional) for item in follower_allocations],
            meta,
        )
        if not follower_targets:
            raise RuntimeError("Follower targets rounded to zero")
        return leader_targets, follower_targets

    async def _staggered_balances(
        self,
        services: list[LighterService],
    ) -> list[Decimal]:
        delays = [0.0]
        for _ in services[1:]:
            delays.append(delays[-1] + pick_range(DELAY_BETWEEN_WALLETS))
        if delays[-1] > 0:
            step("Wallet startup stagger", f"last wallet starts in {delays[-1]:.1f}s")

        async def read(service: LighterService, delay: float) -> Decimal:
            if delay > 0:
                await asyncio.sleep(delay)
            return await asyncio.to_thread(service.available_balance)

        return await asyncio.gather(
            *(read(service, delay) for service, delay in zip(services, delays))
        )

    async def _open_all_market(
        self,
        meta,
        leader_side: str,
        follower_side: str,
        leader_targets: list[PairedTarget],
        follower_targets: list[PairedTarget],
    ) -> None:
        targets = [
            *((target, leader_side) for target in leader_targets),
            *((target, follower_side) for target in follower_targets),
        ]
        books = await asyncio.gather(
            *(
                asyncio.to_thread(target.service.public.book, meta.market_id)
                for target, _ in targets
            )
        )
        await asyncio.gather(
            *(
                target.service.place_market(
                    meta,
                    book,
                    side,
                    target.target_base * book.mid,
                    base_amount_override=target.target_base,
                )
                for (target, side), book in zip(targets, books)
            )
        )

    async def _verify_targets(
        self,
        meta,
        leader_side: str,
        follower_side: str,
        leader_targets: list[PairedTarget],
        follower_targets: list[PairedTarget],
    ) -> list[Decimal]:
        targets = [
            *((target, leader_side) for target in leader_targets),
            *((target, follower_side) for target in follower_targets),
        ]
        positions = await asyncio.gather(
            *(target.service.signed_position(meta.symbol) for target, _ in targets)
        )
        one_unit = Decimal(1) / (Decimal(10) ** meta.size_decimals)
        mismatches = []
        for (target, side), position in zip(targets, positions):
            expected = target.target_base if side == "long" else -target.target_base
            if abs(position - expected) >= one_unit:
                mismatches.append(f"{target.service.label()}={position}/{expected}")
        if mismatches:
            raise RuntimeError(f"Per-wallet open mismatch: {', '.join(mismatches)}")
        net = sum(positions, Decimal(0))
        if abs(net) >= one_unit:
            raise RuntimeError(f"Post-fill net exposure is {net} {meta.symbol}")
        ok("Neutrality check", f"net={net} {meta.symbol}")
        return positions

    async def _send_open_report(
        self,
        cycle_index: int,
        meta,
        book: BookSnapshot,
        allocations: list[Allocation],
    ) -> None:
        lines = [
            f"📂 POSITIONS OPENED | Group {cycle_index}",
            f"📊 {meta.symbol}/USDG | Spread: {book.spread_percent:.4f}% | Mid: ${book.mid:.2f}",
            "",
        ]
        long_total = Decimal(0)
        short_total = Decimal(0)
        for item in allocations:
            position = await item.service.signed_position(meta.symbol)
            notional = abs(position) * book.mid
            if item.side == "long":
                long_total += notional
            else:
                short_total += notional
            marker = "🟢" if item.side == "long" else "🔴"
            lines.append(
                f"{marker} {item.side.upper()} {_tg_service_label(item.service)} | "
                f"${notional:.2f} | Lev: {item.leverage:.1f}x"
            )
        lines.extend(
            ["", f"🟢 LONG: ${long_total:.2f} | 🔴 SHORT: ${short_total:.2f}"]
        )
        await send_tg("\n".join(lines))

    async def _send_close_report(
        self,
        cycle_index: int,
        allocations: list[Allocation],
        balances_before: dict[LighterService, Decimal],
    ) -> None:
        lines = [f"📂 POSITIONS CLOSED | Group {cycle_index}", ""]
        total_pnl = Decimal(0)
        estimated_volume = Decimal(0)
        for item in allocations:
            balance = await asyncio.to_thread(item.service.available_balance)
            pnl = balance - balances_before[item.service]
            total_pnl += pnl
            estimated_volume += item.notional * Decimal(2)
            marker = "🟢" if item.side == "long" else "🔴"
            lines.append(
                f"{marker} {item.side.upper()} {_tg_service_label(item.service)} | "
                f"PnL: {pnl:+.4f}$ | Bal: ${balance:.4f} | "
                f"Cycle vol: ${item.notional * Decimal(2):.2f}"
            )
        cost_per_100k = (
            -total_pnl / estimated_volume * Decimal(100_000)
            if estimated_volume > 0
            else Decimal(0)
        )
        lines.extend(
            [
                "",
                f"📊 Full-cycle balance PnL: {total_pnl:+.4f}$",
                f"💰 Estimated cycle volume: ${estimated_volume:.2f}",
                f"Cost per 100k: ${cost_per_100k:.3f}",
            ]
        )
        await send_tg("\n".join(lines))

    async def trade_cycle(
        self,
        services: list[LighterService],
        cycle_index: int,
    ) -> bool:
        if EXECUTION_MODE not in {"leader-follower", "all-market"}:
            raise RuntimeError("EXECUTION_MODE must be 'leader-follower' or 'all-market'")

        symbol = random.choice(TOKENS_TO_TRADE)
        meta = self._meta(symbol)
        selected: list[LighterService] = []
        allocations: list[Allocation] = []
        leader_targets: list[PairedTarget] = []
        follower_targets: list[PairedTarget] = []
        execution_started = False
        leader_side = "long"
        follower_side = "short"
        balances_before: dict[LighterService, Decimal] = {}
        try:
            group = list(random.choice(GROUP_CONFIGS))
            needed = sum(group)
            if len(services) < needed:
                raise RuntimeError(f"Need {needed} tradable wallets for group {group}, got {len(services)}")
            if random.choice([True, False]):
                group = [group[1], group[0]]

            section(
                "Delta-neutral cycle",
                f"{symbol}/USDG | {EXECUTION_MODE} | LIVE",
            )
            all_balances = await self._staggered_balances(services)
            ranked = sorted(zip(services, all_balances), key=lambda item: item[1], reverse=True)
            selected_accounts = ranked[:needed]
            selected = [service for service, _ in selected_accounts]
            balances_before = dict(selected_accounts)
            info("Group", f"{group[0]} long / {group[1]} short | selected={len(selected)}")
            await self._verify_flat(selected, symbol)
            book = await self._wait_for_spread(meta)

            preferred_source_side = None
            if group[0] == group[1]:
                preferred_source_side = random.choice(["long", "short"])
            leverage_config = TOKEN_LEVERAGE.get(symbol) or TOKEN_LEVERAGE["ETH"]
            leverage_bounds = [
                leverage_config[0],
                min(Decimal(str(leverage_config[1])), meta.max_leverage),
            ]
            maker_min_notional = max(
                meta.min_quote_amount,
                meta.min_base_amount * book.mid,
            )
            allocations = calculate_allocation(
                selected_accounts,
                long_count=group[0],
                leverage_bounds=leverage_bounds,
                percent_bounds=POSITION_PERCENT,
                maker_min_notional=maker_min_notional,
                taker_min_notional=Decimal(0),
                preferred_source_side=preferred_source_side,
            )
            leader_side = max(allocations, key=lambda item: item.notional).side
            follower_side = "short" if leader_side == "long" else "long"
            leader_targets, follower_targets = self._build_targets(
                meta,
                allocations,
                leader_side,
                book,
                validate_maker=EXECUTION_MODE == "leader-follower",
            )
            target_base = sum((target.target_base for target in leader_targets), Decimal(0))
            ok(
                "Sizing",
                f"leader={leader_side.upper()} | follower={follower_side.upper()} | "
                f"base={target_base} {symbol} | approx=${target_base * book.mid:.2f} per side",
            )
            for item in allocations:
                info(
                    item.service.label(),
                    f"{item.side.upper()} | leverage={item.leverage:.1f}x | target=${item.notional:.2f}",
                )

            await asyncio.gather(*(service.cancel_all_orders(meta) for service in selected))
            await asyncio.gather(
                *(item.service.update_leverage(meta, item.leverage) for item in allocations)
            )
            if is_trading_halted():
                raise RuntimeError("Trading halted by Force Close")

            execution_started = True
            if EXECUTION_MODE == "all-market":
                await self._open_all_market(
                    meta,
                    leader_side,
                    follower_side,
                    leader_targets,
                    follower_targets,
                )
            else:
                await execute_paired(
                    meta,
                    leader_side,
                    follower_side,
                    leader_targets,
                    follower_targets,
                    halt_check=is_trading_halted,
                )
            await self._verify_targets(
                meta,
                leader_side,
                follower_side,
                leader_targets,
                follower_targets,
            )
            await self._send_open_report(cycle_index, meta, book, allocations)

            leader_services = [target.service for target in leader_targets]
            follower_services = [target.service for target in follower_targets]
            if all(float(value) == 0 for value in HOLD_MINUTES):
                step("Holding positions", "indefinitely; close via a separate mode 2 run")
                while not is_trading_halted():
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                step("Hold interrupted", "Force Close halt received")
                return False

            hold = pick_range(HOLD_MINUTES)
            step("Holding positions", f"{hold:.2f} minute(s)")
            deadline = asyncio.get_running_loop().time() + hold * 60
            while asyncio.get_running_loop().time() < deadline:
                if is_trading_halted():
                    step("Hold interrupted", "Force Close halt received")
                    return False
                await asyncio.sleep(
                    min(POLL_INTERVAL_SEC, max(0.1, deadline - asyncio.get_running_loop().time()))
                )

            await close_leader_follower(
                leader_services,
                follower_services,
                meta,
            )
            execution_started = False
            await self._send_close_report(cycle_index, allocations, balances_before)
            return True
        except Exception:
            if execution_started and selected:
                warn("Emergency cleanup", f"paired close for {len(selected)} selected wallet(s)")
                try:
                    await close_leader_follower(
                        [target.service for target in leader_targets],
                        [target.service for target in follower_targets],
                        meta,
                    )
                except Exception as cleanup_error:
                    error("Emergency cleanup", exception_summary(cleanup_error))
                    await flatten_positions(selected, meta)
            raise

    async def run_trades(self) -> None:
        services = await self._services(require_trading=True)
        completed = 0
        failures = 0
        await send_tg(f"BOT STARTED | {len(services)} wallet(s) | Delta-neutral mode")
        try:
            while completed < TRADES_COUNT and failures < RETRY:
                if is_trading_halted():
                    break
                section("Cycle", f"{completed + 1}/{TRADES_COUNT}")
                try:
                    cycle_completed = await self.trade_cycle(services, completed + 1)
                    if not cycle_completed:
                        break
                    completed += 1
                    failures = 0
                except Exception as exc:
                    failures += 1
                    error("Strategy failed", f"attempt={failures}/{RETRY} | {exception_summary(exc)}")
                    await send_tg(
                        f"❌ ERROR | Mode 1 | attempt {failures}/{RETRY} | {exception_summary(exc)}"
                    )
                    if failures >= RETRY:
                        raise
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                if completed < TRADES_COUNT and not is_trading_halted():
                    delay = pick_range(DELAY_BETWEEN_TRADES)
                    step("Delay between cycles", f"{delay:.1f}s")
                    await asyncio.sleep(delay)
        finally:
            await send_tg(
                f"BOT STOPPED | Delta-neutral mode | completed={completed}/{TRADES_COUNT}"
            )
            await asyncio.gather(*(service.close() for service in services), return_exceptions=True)
