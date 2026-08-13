# Robinhood Chain Lighter LIT Bot

Delta-neutral style bot scaffold for `https://robinhoodchain.lighter.xyz/trade/LIT`.

This is a Lighter/Robinhood Chain build, not a Phoenix/Solana drop-in. Lighter requires:

- `account_index`
- `api_key_index`
- Lighter API private key

The Phoenix `privatekeys.txt` Solana wallet format cannot sign Lighter orders.

## Setup

```powershell
cd D:\soft\lighter-robinhood-lit
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .\input_data\accounts.example.csv .\input_data\accounts.csv
```

Edit `input_data/accounts.csv`.

Keep `DRY_RUN = True` in `settings.py` for the first run. Mode `4` works without accounts.

## Run

```powershell
python main.py
```

Or, if you prefer the same command style as the Phoenix bot:

```powershell
npm start
```

Modes:

- `1` open one LIT delta-neutral cycle from configured accounts
- `2` close configured LIT positions with reduce-only market orders
- `3` check balances and LIT positions
- `4` inspect live LIT market metadata/order book
- `5` cancel configured LIT orders

To send live orders, set `DRY_RUN = False` in `settings.py`.
