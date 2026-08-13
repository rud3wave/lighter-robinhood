import csv
import random
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AccountConfig:
    name: str
    account_index: int
    api_key_index: int
    api_private_key: str


def load_accounts(path: Path, shuffle: bool = True) -> list[AccountConfig]:
    if not path.exists():
        raise FileNotFoundError(f"Missing accounts file: {path}")

    accounts: list[AccountConfig] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if not row or not row.get("account_index"):
                continue
            private_key = (row.get("api_private_key") or "").strip()
            if not private_key or private_key.startswith("PASTE_"):
                continue
            account_index = int((row.get("account_index") or "").strip())
            accounts.append(
                AccountConfig(
                    name=(row.get("name") or str(account_index)).strip(),
                    account_index=account_index,
                    api_key_index=int((row.get("api_key_index") or "2").strip()),
                    api_private_key=private_key,
                )
            )

    if shuffle:
        random.shuffle(accounts)
    return accounts

