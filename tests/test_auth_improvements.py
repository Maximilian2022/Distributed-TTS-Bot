import unittest
import time
import json
import base64
import hmac
import hashlib
from unittest.mock import MagicMock, AsyncMock, patch
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from src.web.auth import DiscordAuth


class TestAuthImprovements(unittest.TestCase):
    """OAuth2改善、マジックリンク、アプリ承認ログイン（番号確認付き）の単体テスト"""

    def setUp(self):
        self.mock_bot = MagicMock()
        self.auth = DiscordAuth(
            client_id="test_client_id",
            client_secret="test_client_secret",
            redirect_uri="https://example.com/callback",
            session_secret="test_session_secret_12345",
            bot=self.mock_bot
        )

    # ---------------------------------------------------------
    # 1. 通常 OAuth2 & セッションCookie 改善
    # ---------------------------------------------------------
    def test_get_login_url_prompt(self):
        # デフォルトは prompt=none
        url_none = self.auth.get_login_url(state="test_state_1", prompt="none")
        self.assertIn("prompt=none", url_none)
        self.assertIn("client_id=test_client_id", url_none)
        self.assertIn("state=test_state_1", url_none)

        # 明示的 consent
        url_consent = self.auth.get_login_url(state="test_state_2", prompt="consent")
        self.assertIn("prompt=consent", url_consent)

    def test_session_token_30_days(self):
        user_data = {"id": 123456789, "username": "TestUser", "global_name": "Tester"}
        token = self.auth.create_session_token(user_data, max_age=86400 * 30, is_admin=True)
        self.assertIsNotNone(token)

        verified = self.auth.verify_session_token(token)
        self.assertIsNotNone(verified)
        self.assertEqual(verified["user_id"], 123456789)
        self.assertEqual(verified["username"], "TestUser")
        self.assertTrue(verified["is_admin"])
        # 有効期限が約30日後であること
        now = time.time()
        self.assertGreater(verified["exp"], now + 86400 * 29)
        self.assertLessEqual(verified["exp"], now + 86400 * 30 + 10)

    # ---------------------------------------------------------
    # 2. 【A案】本人専用ワンタイム・マジックリンク
    # ---------------------------------------------------------
    def test_magic_login_token_lifecycle(self):
        user_id = 987654321
        username = "Alice"
        token = self.auth.create_magic_login_token(
            user_id=user_id,
            username=username,
            is_admin=True,
            target_path="/",
            expires_in=600
        )
        self.assertTrue(token.startswith("mgl_"))

        # 初回消費: 成功
        payload = self.auth.verify_and_consume_magic_login_token(token)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["user_id"], user_id)
        self.assertEqual(payload["username"], username)
        self.assertTrue(payload["is_admin"])

        # 2回目消費: 単一消費済みのため失敗
        consumed = self.auth.verify_and_consume_magic_login_token(token)
        self.assertIsNone(consumed)

    def test_magic_login_token_tampered(self):
        token = self.auth.create_magic_login_token(user_id=111, username="Bob")
        parts = token.split(".")
        # 署名を改ざん
        tampered_token = parts[0] + ".tampered_signature"
        self.assertIsNone(self.auth.verify_and_consume_magic_login_token(tampered_token))

    def test_magic_login_token_expired(self):
        # 過去の期限で手動作成
        payload = {
            "type": "magic_login",
            "user_id": 222,
            "username": "Charlie",
            "is_admin": False,
            "path": "/",
            "exp": int(time.time()) - 100,
            "nonce": "test_expired_nonce"
        }
        raw_json = json.dumps(payload, separators=(',', ':'))
        b64_payload = base64.urlsafe_b64encode(raw_json.encode()).decode()
        sig = hmac.new(self.auth.session_secret, b64_payload.encode(), hashlib.sha256).hexdigest()
        expired_token = f"mgl_{b64_payload}.{sig}"

        self.assertIsNone(self.auth.verify_and_consume_magic_login_token(expired_token))

    # ---------------------------------------------------------
    # 3. 【B案】Discordアプリ ワンタップ承認ログイン (番号確認付き)
    # ---------------------------------------------------------
    def test_app_login_request_and_number_matching(self):
        user_id = 55555
        username = "Dave"
        client_ip = "192.168.1.50"
        user_agent = "Mozilla/5.0 Test Browser"

        req_id, verify_code = self.auth.create_app_login_request(
            user_id=user_id,
            username=username,
            ip=client_ip,
            user_agent=user_agent,
            expires_in=120
        )
        self.assertIsNotNone(req_id)
        self.assertIsNotNone(verify_code)
        self.assertTrue(verify_code.isdigit())
        self.assertEqual(len(verify_code), 2)  # 2桁の番号一致コード

        # ステータス確認
        stat = self.auth.get_app_login_status(req_id)
        self.assertEqual(stat["status"], "pending")

        # 番号不一致での承認試行 -> 失敗すること
        dummy_session = "dummy_session_token"
        wrong_code = "99" if verify_code != "99" else "88"
        fail_res = self.auth.approve_app_login_request(req_id, dummy_session, verify_code=wrong_code)
        self.assertFalse(fail_res)

        # 正しい番号での承認 -> 成功すること
        ok_res = self.auth.approve_app_login_request(req_id, dummy_session, verify_code=verify_code)
        self.assertTrue(ok_res)

        # 確定後のステータス確認
        stat_after = self.auth.get_app_login_status(req_id)
        self.assertEqual(stat_after["status"], "approved")
        self.assertEqual(stat_after["session_token"], dummy_session)

    def test_app_login_rejection(self):
        text_name = "Eve"
        req_id, verify_code = self.auth.create_app_login_request(
            user_id=777,
            username=text_name,
            ip="192.168.1.60",
            user_agent="Test UA"
        )
        self.assertTrue(self.auth.reject_app_login_request(req_id))
        stat = self.auth.get_app_login_status(req_id)
        self.assertEqual(stat["status"], "rejected")

    def test_app_login_rate_limits(self):
        ip = "192.168.1.70"
        # 1分間に3回まで許可
        self.assertTrue(self.auth.check_ip_rate_limit(ip, max_requests=3, window_sec=60.0))
        self.assertTrue(self.auth.check_ip_rate_limit(ip, max_requests=3, window_sec=60.0))
        self.assertTrue(self.auth.check_ip_rate_limit(ip, max_requests=3, window_sec=60.0))
        # 4回目はブロック
        self.assertFalse(self.auth.check_ip_rate_limit(ip, max_requests=3, window_sec=60.0))

    def test_app_login_user_cooldown(self):
        user_id = 9999
        self.assertTrue(self.auth.can_request_app_login_for_user(user_id, cooldown_sec=60.0))
        # リクエスト作成でクールダウン開始
        self.auth.create_app_login_request(user_id, "Frank", "127.0.0.1", "UA")
        # 直後はクールダウン中
        self.assertFalse(self.auth.can_request_app_login_for_user(user_id, cooldown_sec=60.0))


class TestWebServerAuthHandlers(AioHTTPTestCase):
    """WebServer の認証関連エンドポイントテスト"""

    async def get_application(self):
        from src.web.server import WebServer
        from src.config import AppConfig

        config = AppConfig()
        config.discord_client_id = "test_id"
        config.discord_client_secret = "test_secret"
        config.discord_redirect_uri = "https://example.com/callback"
        config.web_session_secret = "test_secret_for_server"

        mock_bot = MagicMock()
        mock_bot.guilds = []

        mock_db = MagicMock()
        mock_db.get_credential = AsyncMock(return_value="")
        mock_db.set_credential = AsyncMock(return_value=True)
        mock_db.get_worker_settings = AsyncMock(return_value={})
        mock_db.get_all_worker_settings = AsyncMock(return_value={})

        mock_tts = MagicMock()
        mock_cache = MagicMock()

        self.web_server = WebServer(
            config=config,
            database=mock_db,
            cache_manager=mock_cache,
            tts_manager=mock_tts,
            bot=mock_bot
        )
        return self.web_server.app

    @unittest_run_loop
    async def test_callback_consent_fallback(self):
        # prompt=none の失敗時 (error=interaction_required) に prompt=consent へ自動フォールバックすること
        resp = await self.client.get("/callback?error=interaction_required", allow_redirects=False)
        self.assertEqual(resp.status, 302)
        location = resp.headers.get("Location", "")
        self.assertIn("prompt=consent", location)
        self.assertIn("client_id=test_id", location)

    @unittest_run_loop
    async def test_magic_login_endpoint(self):
        # 有効なマジックリンクトークンで /magic を開いた場合
        token = self.web_server.auth.create_magic_login_token(
            user_id=12345,
            username="TestUser",
            is_admin=True,
            target_path="/"
        )
        resp = await self.client.get(f"/magic?token={token}", allow_redirects=False)
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.headers.get("Location"), "/")
        # 30日の ttsbot_session Cookie がセットされていること
        cookies = resp.cookies
        self.assertIn("ttsbot_session", cookies)
        cookie_val = cookies["ttsbot_session"].value
        user = self.web_server.auth.verify_session_token(cookie_val)
        self.assertIsNotNone(user)
        self.assertEqual(user["user_id"], 12345)
        self.assertTrue(user["is_admin"])

    @unittest_run_loop
    async def test_auth_config_endpoint(self):
        resp = await self.client.get("/api/auth/config")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["is_configured"])
        self.assertEqual(data["client_id"], "test_id")
        self.assertIn("/callback", data["dynamic_redirect_uri"])

    @unittest_run_loop
    async def test_magic_login_open_redirect_prevention(self):
        # 悪意あるオープンリダイレクトURL (//attacker.com, https://...) を遮断し安全な / へ遷移すること
        malicious_paths = [
            "https://attacker.example.com",
            "//attacker.example.com/steal",
            "/\\attacker.example.com",
            "javascript:alert(1)"
        ]
        for bad_path in malicious_paths:
            token = self.web_server.auth.create_magic_login_token(
                user_id=12345,
                username="TestUser",
                is_admin=False,
                target_path=bad_path
            )
            resp = await self.client.get(f"/magic?token={token}", allow_redirects=False)
            self.assertEqual(resp.status, 302)
            # 外部URLではなく安全なルート / にリダイレクトされること
            self.assertEqual(resp.headers.get("Location"), "/")


if __name__ == "__main__":
    unittest.main()
