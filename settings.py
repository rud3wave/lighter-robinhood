"""Trading settings for Robinhood Chain Lighter LIT bot."""

API_BASE_URL = "https://api.rh.lighter.xyz"
TRADE_URL = "https://robinhoodchain.lighter.xyz/trade/LIT"
SYMBOL = "LIT"

# Keep this True until balances, account indexes, and order sizes look right.
DRY_RUN = True

SHUFFLE_ACCOUNTS = True
RETRY = 3

# LIT on Robinhood Lighter currently allows up to 5x by min_initial_margin_fraction.
TOKEN_LEVERAGE = {
    "LIT": [2, 4],
}

# Percentage of account available balance used as margin before leverage.
POSITION_PERCENT = [20, 30]

# [longCount, shortCount]. Must match the number of configured accounts.
GROUP_CONFIGS = [[1, 1]]

MAX_SPREAD = 0.05
SLIPPAGE = 0.03
HOLD_MINUTES = [0, 0]
TRADES_COUNT = 1
DELAY_BETWEEN_TRADES = [10, 30]

