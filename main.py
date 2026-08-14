import asyncio

from lighter_bot.controller import Controller
from lighter_bot.pretty import banner, error, exception_summary, info, paint, C
from lighter_bot.runtime_control import (
    acquire_trading_lock,
    clear_trading_halt,
    release_trading_lock,
    request_trading_halt,
)
from lighter_bot.telegram import send_tg
from settings import TRADE_URL


def print_menu() -> None:
    print()
    print(paint("Выбери режим:", C.BOLD + C.WHITE))
    print(f"  {paint('1', C.CYAN)} = Дельта-нейтральная торговля")
    print(f"  {paint('2', C.CYAN)} = Закрыть все позиции")
    print(f"  {paint('3', C.CYAN)} = Проверить балансы")
    print(f"  {paint('4', C.CYAN)} = Регистрация по рефке + пополнение USDG")
    print(f"  {paint('0', C.GRAY)} = Выход")


def print_stopped() -> None:
    print()
    info("Завершено", "процесс остановлен пользователем")


async def main() -> None:
    banner()
    info("Target", TRADE_URL)
    info("Trading LIVE", "modes 1-2 send transactions")
    info("Mode 4", "live USDG deposit starts immediately")
    controller = Controller()
    print_menu()
    choice = input(paint("> ", C.BOLD + C.CYAN)).strip()
    lock_acquired = False
    try:
        if choice == "1":
            acquire_trading_lock()
            lock_acquired = True
            clear_trading_halt()
            await controller.run_trades()
        elif choice == "2":
            request_trading_halt()
            await controller.close_positions()
        elif choice == "3":
            await controller.balances()
        elif choice == "4":
            await controller.deposit_from_wallets()
        elif choice == "0":
            return
        else:
            raise RuntimeError(f'Неизвестный режим: "{choice}"')
    except asyncio.CancelledError:
        if choice == "1":
            request_trading_halt()
        print_stopped()
    except (EOFError, KeyboardInterrupt):
        if choice == "1":
            request_trading_halt()
        print_stopped()
    except Exception as exc:
        error("Ошибка режима", exception_summary(exc))
        await send_tg(f"❌ ERROR | Mode {choice} | {exception_summary(exc)}")
    finally:
        if lock_acquired:
            release_trading_lock()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (EOFError, KeyboardInterrupt):
        print_stopped()
    except Exception as exc:
        error("Fatal error", exception_summary(exc))
