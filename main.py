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


def print_menu() -> None:
    print()
    print(paint("Выбери режим:", C.BOLD + C.WHITE))
    print(f"  {paint('1', C.CYAN)} = Дельта-нейтральная торговля")
    print(f"  {paint('2', C.CYAN)} = Закрыть все позиции")
    print(f"  {paint('3', C.CYAN)} = Проверить балансы")
    print(f"  {paint('4', C.CYAN)} = Регистрация + пополнение USDG")
    print(f"  {paint('5', C.CYAN)} = Вывод средств")
    print(f"  {paint('0', C.GRAY)} = Выход")


def choose_withdraw_method() -> str | None:
    print()
    print(paint("Выбери способ вывода:", C.BOLD + C.WHITE))
    print(f"  {paint('1', C.CYAN)} = Fast   - 15-20 сек, возможна комиссия")
    print(f"  {paint('2', C.CYAN)} = Secure - около часа, Lighter отправит сам")
    print(f"  {paint('0', C.GRAY)} = Выход")
    choice = input(paint("> ", C.BOLD + C.CYAN)).strip()
    if choice == "1":
        return "fast"
    if choice == "2":
        return "secure"
    if choice == "0":
        return None
    raise RuntimeError(f'Неизвестный способ вывода: "{choice}"')


def print_stopped() -> None:
    print()
    info("Завершено", "процесс остановлен пользователем")


async def main() -> None:
    banner()
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
        elif choice == "5":
            withdraw_method = choose_withdraw_method()
            if withdraw_method is None:
                return
            await controller.withdraw_to_wallets(withdraw_method)
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
        await send_tg(f"❌ ОШИБКА | Режим {choice} | {exception_summary(exc)}")
    finally:
        if lock_acquired:
            release_trading_lock()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (EOFError, KeyboardInterrupt):
        print_stopped()
    except Exception as exc:
        error("Ошибка запуска", exception_summary(exc))
