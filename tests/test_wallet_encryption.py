from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lighter_bot.vault import seal_line, seal_text
from lighter_bot.wallets import (
    WalletAccount,
    address_from_private_key,
    decrypt_key,
    load_wallets_db,
    read_private_keys,
    read_proxies,
    save_wallets,
)


CRYPTOJS_FIXTURE = (
    "U2FsdGVkX183AzKUXRHCqfIXP0EjENgzyLK2U643FoeiAYfJDv8cWgCs4bSGqg05"
    "gk9oGj2/utAQSEyDGA31/7v5lDQm5KRapw7P+Qcft4NARRpVFKnPk9HmsIZtpvb5"
)


class WalletEncryptionTests(unittest.TestCase):
    def test_decrypts_cryptojs_phoenix_format(self) -> None:
        with patch("lighter_bot.wallets._encryption_password", return_value="test-password"):
            decrypted = decrypt_key(CRYPTOJS_FIXTURE)

        self.assertEqual(decrypted, "0x" + "1" * 64)

    def test_database_encrypts_keys_but_not_proxy(self) -> None:
        wallet = WalletAccount(
            index=0,
            private_key="0x" + "1" * 64,
            address="0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A",
            proxy_url="http://user:password@proxy.example:8000",
            account_index=123,
            api_private_key="lighter-api-private-key",
        )

        with tempfile.TemporaryDirectory() as directory:
            db_dir = Path(directory) / "databases"
            db_path = db_dir / "wallets.json"
            with (
                patch("lighter_bot.wallets.DB_DIR", db_dir),
                patch("lighter_bot.wallets.WALLETS_DB", db_path),
                patch("lighter_bot.wallets._encryption_password", return_value="test-password"),
            ):
                save_wallets([wallet])
                raw = db_path.read_text(encoding="utf-8")
                loaded = load_wallets_db()

        document = json.loads(raw)
        self.assertIsInstance(document, list)
        self.assertIn(wallet.proxy_url, raw)
        self.assertNotIn(wallet.private_key, raw)
        self.assertNotIn(wallet.api_private_key, raw)
        self.assertTrue(document[0]["encryptedKey"].startswith("U2FsdGVkX1"))
        self.assertEqual(loaded, [wallet])

    def test_filtered_update_preserves_other_wallet_records(self) -> None:
        wallets = [
            WalletAccount(
                index=index,
                private_key="0x" + str(index + 1) * 64,
                address=(
                    "0x19E7E376E7C213B7E7e46cc70A5dD086DAff2A"
                    if index == 0
                    else "0x1563915e194D8CfBA1943570603F7606A3115508"
                ),
            )
            for index in range(2)
        ]
        for wallet in wallets:
            wallet.address = address_from_private_key(wallet.private_key)
        wallets[1].account_index = 456

        with tempfile.TemporaryDirectory() as directory:
            db_dir = Path(directory) / "databases"
            db_path = db_dir / "wallets.json"
            with (
                patch("lighter_bot.wallets.DB_DIR", db_dir),
                patch("lighter_bot.wallets.WALLETS_DB", db_path),
                patch("lighter_bot.wallets._encryption_password", return_value="test-password"),
            ):
                save_wallets(wallets)
                wallets[0].account_index = 123
                save_wallets([wallets[0]], preserve_existing=True)
                loaded = load_wallets_db() or []

        self.assertEqual(len(loaded), 2)
        self.assertEqual([item.account_index for item in loaded], [123, 456])

    def test_legacy_encrypted_inputs_are_migrated_to_plaintext(self) -> None:
        private_key = "0x" + "1" * 64
        proxy = "http://user:password@proxy.example:8000"

        with tempfile.TemporaryDirectory() as directory:
            private_path = Path(directory) / "privatekeys.txt"
            proxy_path = Path(directory) / "proxies.txt"
            with patch("lighter_bot.vault.vault_password", return_value="legacy-password"):
                private_path.write_text(
                    seal_line(private_key, "input:privatekeys") + "\n",
                    encoding="utf-8",
                )
                proxy_path.write_text(
                    seal_line(proxy, "input:proxies") + "\n",
                    encoding="utf-8",
                )

                with (
                    patch("lighter_bot.wallets.PRIVATE_KEYS_PATH", private_path),
                    patch("lighter_bot.wallets.PROXIES_PATH", proxy_path),
                ):
                    self.assertEqual(read_private_keys(), [private_key])
                    self.assertEqual(read_proxies(), [proxy])

            private_text = private_path.read_text(encoding="utf-8")
            proxy_text = proxy_path.read_text(encoding="utf-8")

        self.assertIn(private_key, private_text)
        self.assertIn(proxy, proxy_text)
        self.assertNotIn("enc:v2:", private_text)
        self.assertNotIn("enc:v2:", proxy_text)

    def test_v2_database_is_loaded_and_resaved_in_phoenix_format(self) -> None:
        wallet_data = {
            "index": 0,
            "private_key": "0x" + "1" * 64,
            "address": "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A",
            "proxy_url": "http://proxy.example:8000",
            "account_index": 123,
            "api_key_index": 4,
            "api_private_key": "lighter-api-private-key",
        }
        record_id = "legacy-record"

        with tempfile.TemporaryDirectory() as directory:
            db_dir = Path(directory) / "databases"
            db_dir.mkdir()
            db_path = db_dir / "wallets.json"
            with patch("lighter_bot.vault.vault_password", return_value="legacy-password"):
                payload = seal_text(
                    json.dumps(wallet_data, separators=(",", ":"), sort_keys=True),
                    f"wallet-record:{record_id}",
                )
                db_path.write_text(
                    json.dumps(
                        {
                            "version": 2,
                            "wallets": [{"id": record_id, "payload": payload}],
                        }
                    ),
                    encoding="utf-8",
                )

                with (
                    patch("lighter_bot.wallets.DB_DIR", db_dir),
                    patch("lighter_bot.wallets.WALLETS_DB", db_path),
                    patch(
                        "lighter_bot.wallets._encryption_password",
                        return_value="legacy-password",
                    ),
                ):
                    loaded = load_wallets_db()
                    self.assertIsNotNone(loaded)
                    save_wallets(loaded or [])

            migrated = json.loads(db_path.read_text(encoding="utf-8"))

        self.assertIsInstance(migrated, list)
        self.assertIn("encryptedKey", migrated[0])
        self.assertEqual(migrated[0]["proxyUrl"], wallet_data["proxy_url"])


if __name__ == "__main__":
    unittest.main()
