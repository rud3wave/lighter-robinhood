import asyncio

from lighter_bot.controller import Controller
from settings import DRY_RUN, TRADE_URL


MENU = """
Robinhood Chain Lighter LIT
1 = Delta-neutral open cycle
2 = Close all configured LIT positions
3 = Check balances
4 = Market info
5 = Cancel all configured LIT orders
0 = Exit
"""


async def main() -> None:
    print(f"Target: {TRADE_URL}")
    print(f"DRY_RUN={DRY_RUN}")
    controller = Controller()
    while True:
        print(MENU)
        choice = input("Choose mode: ").strip()
        try:
            if choice == "1":
                await controller.run_trades()
            elif choice == "2":
                await controller.close_positions()
            elif choice == "3":
                await controller.balances()
            elif choice == "4":
                controller.market_info()
            elif choice == "5":
                await controller.cancel_all()
            elif choice == "0":
                return
            else:
                print("Unknown mode")
        except Exception as exc:
            print(f"Error: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
