import unittest
import time
import base64
import json
import hmac
import hashlib
from src.web.auth import DiscordAuth

class TestDiscordAuth(unittest.TestCase):
    def setUp(self):
        self.auth = DiscordAuth(
            client_id="dummy_id",
            client_secret="dummy_secret",
            redirect_uri="http://localhost:8088/auth/callback",
            session_secret="super_secret_current_key",
            bot=None
        )
        self.user_data = {
            "id": "1234567890",
            "username": "test_user",
            "global_name": "Test User",
            "avatar": "avatar_hash"
        }

    def test_session_token_creation_and_verification(self):
        # 1. 一般ユーザーのトークン
        token = self.auth.create_session_token(self.user_data, max_age=3600, is_admin=False)
        self.assertIsNotNone(token)
        self.assertIn(".", token)

        payload = self.auth.verify_session_token(token)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["user_id"], 1234567890)
        self.assertEqual(payload["username"], "test_user")
        self.assertFalse(payload["is_admin"])

        # 2. 管理者ユーザーのトークン
        admin_token = self.auth.create_session_token(self.user_data, max_age=3600, is_admin=True)
        admin_payload = self.auth.verify_session_token(admin_token)
        self.assertIsNotNone(admin_payload)
        self.assertTrue(admin_payload["is_admin"])

    def test_session_token_tampering(self):
        token = self.auth.create_session_token(self.user_data, max_age=3600)
        b64_payload, signature = token.split(".", 1)

        # ペイロード改ざん (is_adminをTrueに改ざん)
        decoded = json.loads(base64.urlsafe_b64decode(b64_payload.encode()).decode())
        decoded["is_admin"] = True
        tampered_b64 = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode()
        tampered_token = f"{tampered_b64}.{signature}"

        self.assertIsNone(self.auth.verify_session_token(tampered_token))

        # シグネチャ改ざん
        bad_sig_token = f"{b64_payload}.{'a' * len(signature)}"
        self.assertIsNone(self.auth.verify_session_token(bad_sig_token))

    def test_session_token_expiration(self):
        # 期限切れトークン作成 (max_age = -10秒)
        expired_token = self.auth.create_session_token(self.user_data, max_age=-10)
        self.assertIsNone(self.auth.verify_session_token(expired_token))

    def test_session_token_rotation_fallback(self):
        old_secret = "legacy_secret_key"
        current_secret = "new_secret_key"

        # 旧シークレットで署名したトークンを作成
        old_auth = DiscordAuth(
            client_id="dummy_id",
            client_secret="dummy_secret",
            redirect_uri="http://localhost:8088/auth/callback",
            session_secret=old_secret,
            bot=None
        )
        old_token = old_auth.create_session_token(self.user_data, max_age=3600)

        # 新シークレットを持ち、旧シークレットをフォールバックに登録したインスタンス
        new_auth = DiscordAuth(
            client_id="dummy_id",
            client_secret="dummy_secret",
            redirect_uri="http://localhost:8088/auth/callback",
            session_secret=current_secret,
            bot=None,
            old_session_secrets=[old_secret]
        )

        # 旧トークンが正常に検証・復元されること
        verified = new_auth.verify_session_token(old_token)
        self.assertIsNotNone(verified)
        self.assertEqual(verified["user_id"], 1234567890)
        self.assertEqual(verified["username"], "test_user")

if __name__ == "__main__":
    unittest.main()
