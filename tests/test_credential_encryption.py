import os
import unittest
import tempfile
import asyncio
from pathlib import Path
from src.utils.security import encrypt_credential, decrypt_credential, get_ha_secret_key
from src.database import Database
from src.config import AppConfig


class TestCredentialEncryption(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.test_dir.name) / "test.db"
        self.db = Database(self.db_path)
        await self.db.init_db()
        self.secret_key = "test_cluster_secret_key_2026"
        os.environ["TTSBOT_HA_SECRET"] = self.secret_key

    async def asyncTearDown(self):
        await self.db.close()
        self.test_dir.cleanup()

    def test_encrypt_decrypt_basic(self):
        plain = "discord_token_secret_12345!@#"
        cipher = encrypt_credential(plain, self.secret_key)
        self.assertTrue(cipher.startswith("enc:v1:"))
        self.assertNotEqual(plain, cipher)

        decrypted = decrypt_credential(cipher, self.secret_key)
        self.assertEqual(decrypted, plain)

    def test_key_mismatch(self):
        plain = "super_confidential_api_key"
        cipher = encrypt_credential(plain, "correct_secret_key")
        wrong_decrypted = decrypt_credential(cipher, "wrong_secret_key")
        self.assertEqual(wrong_decrypted, "")

    def test_plaintext_backward_compatibility(self):
        plain_legacy = "plain_unencrypted_legacy_token"
        result = decrypt_credential(plain_legacy, self.secret_key)
        self.assertEqual(result, plain_legacy)

    def test_double_encryption_prevention(self):
        plain = "test_double_enc"
        cipher1 = encrypt_credential(plain, self.secret_key)
        cipher2 = encrypt_credential(cipher1, self.secret_key)
        self.assertEqual(cipher1, cipher2)

    async def test_database_credential_encryption(self):
        raw_secret = "azure_speech_key_sample_value"
        await self.db.set_credential("azure_speech_key", raw_secret, is_secret=True)

        # 生のDB値が暗号化されていることを検証 (at-rest 暗号化)
        async with self.db.get_connection() as conn:
            async with conn.execute("SELECT value FROM system_credentials WHERE key = 'azure_speech_key'") as cur:
                row = await cur.fetchone()
                self.assertIsNotNone(row)
                stored_val = str(row[0])
                self.assertTrue(stored_val.startswith("enc:v1:"))
                self.assertNotEqual(stored_val, raw_secret)

        # 通常取得時は透過的に復号されること
        fetched = await self.db.get_credential("azure_speech_key")
        self.assertEqual(fetched, raw_secret)

        # decrypt=False の場合は暗号文がそのまま取得できること
        raw_fetched = await self.db.get_credential("azure_speech_key", decrypt=False)
        self.assertEqual(raw_fetched, stored_val)

    async def test_database_bot_token_encryption(self):
        raw_token = "MTA1OTk3NTcwOT.sample.token.999"
        await self.db.save_bot_token(1, raw_token, label="Bot1号機", is_enabled=True)

        # 生のDB値が暗号化されていることを検証
        async with self.db.get_connection() as conn:
            async with conn.execute("SELECT token FROM bot_tokens WHERE bot_index = 1") as cur:
                row = await cur.fetchone()
                self.assertIsNotNone(row)
                stored_token = str(row[0])
                self.assertTrue(stored_token.startswith("enc:v1:"))
                self.assertNotEqual(stored_token, raw_token)

        # get_bot_tokens() で自動復号されること
        tokens = await self.db.get_bot_tokens()
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0]["token"], raw_token)
        self.assertEqual(tokens[0]["label"], "Bot1号機")

    async def test_load_and_seed_system_credentials(self):
        cfg = AppConfig()
        cfg.discord_client_id = ""
        cfg.discord_client_secret = ""

        # 1. Masterシード動作: 環境変数あり、DB未登録
        os.environ["DISCORD_CLIENT_ID"] = "1122334455"
        os.environ["DISCORD_CLIENT_SECRET"] = "secret_client_xyz"
        seeded = await self.db.load_and_seed_system_credentials(cfg)
        self.assertEqual(seeded.get("discord_client_id"), "1122334455")
        self.assertEqual(seeded.get("discord_client_secret"), "secret_client_xyz")

        # DB内に暗号化保存されたことを確認
        async with self.db.get_connection() as conn:
            async with conn.execute("SELECT value FROM system_credentials WHERE key = 'discord_client_secret'") as cur:
                row = await cur.fetchone()
                self.assertTrue(str(row[0]).startswith("enc:v1:"))

        # 2. Slave展開動作: 環境変数が未設定の状態でDBから動的ロード
        os.environ.pop("DISCORD_CLIENT_ID", None)
        os.environ.pop("DISCORD_CLIENT_SECRET", None)
        cfg.discord_client_id = ""
        cfg.discord_client_secret = ""

        loaded = await self.db.load_and_seed_system_credentials(cfg)
        self.assertEqual(loaded.get("discord_client_id"), "1122334455")
        self.assertEqual(loaded.get("discord_client_secret"), "secret_client_xyz")
        # os.environ に展開されていること
        self.assertEqual(os.environ.get("DISCORD_CLIENT_ID"), "1122334455")
        self.assertEqual(os.environ.get("DISCORD_CLIENT_SECRET"), "secret_client_xyz")
        # cfg に展開されていること
        self.assertEqual(cfg.discord_client_id, "1122334455")
        self.assertEqual(cfg.discord_client_secret, "secret_client_xyz")


if __name__ == "__main__":
    unittest.main()

