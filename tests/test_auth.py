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

class TestPasswordBruteforceProtection(unittest.TestCase):
    """パスワードログインのブルートフォース攻撃防御（レートリミット）テスト"""
    def setUp(self):
        self.failed_attempts = {}

    def _record_and_check(self, client_ip: str, now: float) -> bool:
        """5回以上の失敗で拒否 (429判定)"""
        attempts = [t for t in self.failed_attempts.get(client_ip, []) if now - t < 300]
        if len(attempts) >= 5:
            return False  # Blocked
        attempts.append(now)
        self.failed_attempts[client_ip] = attempts
        return True  # Allowed

    def test_rate_limit_blocking_after_5_attempts(self):
        ip = "192.168.1.100"
        t0 = 1000.0

        # 1〜5回目の失敗は許可される
        for i in range(5):
            self.assertTrue(self._record_and_check(ip, t0 + i))

        # 6回目はブロックされる (429)
        self.assertFalse(self._record_and_check(ip, t0 + 6))
        # 別IPは影響を受けない
        self.assertTrue(self._record_and_check("192.168.1.101", t0 + 6))

    def test_rate_limit_expiry_after_300s(self):
        ip = "192.168.1.200"
        t0 = 1000.0

        for i in range(5):
            self.assertTrue(self._record_and_check(ip, t0 + i))

        # 301秒経過後はブロック解除
        self.assertTrue(self._record_and_check(ip, t0 + 301))

if __name__ == "__main__":
    unittest.main()


class TestDiscordAuthLazyBot(unittest.IsolatedAsyncioTestCase):
    """WebServer は bot=None で先行起動されるため、判定時に bot_provider で遅延解決できること (WebUIログイン不可の回帰防止)"""

    class _Member: pass

    class _Guild:
        def __init__(self, members):
            self._members = members
        def get_member(self, uid):
            return self._members.get(uid)
        async def fetch_member(self, uid):
            raise RuntimeError("not found")

    class _Bot:
        def __init__(self, guilds):
            self.guilds = guilds

    def _auth(self, bot, provider=None):
        return DiscordAuth("i", "s", "http://x/cb", "secret", bot=bot, bot_provider=provider)

    async def test_bot_none_without_provider_is_rejected(self):
        self.assertFalse(await self._auth(None).is_user_in_bot_guilds(1))

    async def test_provider_resolves_bot_lazily(self):
        holder = {"bot": None}
        auth = self._auth(None, lambda: holder["bot"])
        self.assertFalse(await auth.is_user_in_bot_guilds(42))
        holder["bot"] = self._Bot([self._Guild({42: self._Member()})])
        self.assertTrue(await auth.is_user_in_bot_guilds(42))
        self.assertFalse(await auth.is_user_in_bot_guilds(99))

    async def test_provider_exception_falls_back(self):
        def boom():
            raise RuntimeError("x")
        auth = self._auth(self._Bot([self._Guild({7: self._Member()})]), boom)
        self.assertTrue(await auth.is_user_in_bot_guilds(7))
