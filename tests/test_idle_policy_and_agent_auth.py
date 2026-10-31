"""エンジン自動停止の設定 (保存・権限・配信) と、Worker Agent API の認証"""
import importlib.util
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from src.config import AppConfig
from src.database import Database
from src.utils import worker_auth
from src.web.server import WebServer


class TestIdlePolicySettings(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = AppConfig()
        config.worker_shared_secret = "shared_secret_x"
        config.db_path = os.path.join(self.tmp.name, "bot.db")
        self.db = Database(config.db_path)
        await self.db.init_db()
        tts = MagicMock()
        tts.ws_worker_nodes = {}
        for pool in ("voicevox_pool", "sharevox_pool", "coeiroink_pool"):
            getattr(tts, pool).nodes = []
        self.web_server = WebServer(bot=MagicMock(), config=config, database=self.db,
                                    cache_manager=MagicMock(), tts_manager=tts)
        return self.web_server.app

    async def tearDownAsync(self):
        await self.db.close()
        self.tmp.cleanup()

    def _cookie(self, user_id, admin=False):
        return {"ttsbot_session": self.web_server.auth.create_session_token({"id": user_id, "username": f"u{user_id}"}, is_admin=admin)}

    @unittest_run_loop
    async def test_payload_mapping(self):
        ws = self.web_server
        self.assertIsNone(await ws._idle_policy_payload("worker_A"))
        await self.db.upsert_worker_settings("worker_A", "never", 15, 1)
        await self.db.upsert_worker_settings("worker_B", "minutes", 60, 1)
        await self.db.upsert_worker_settings("worker_C", "default", 15, 1)
        ws._worker_settings_cache = None
        self.assertEqual(await ws._idle_policy_payload("worker_A"), {"enabled": False})
        self.assertEqual(await ws._idle_policy_payload("worker_B"), {"enabled": True, "timeout_sec": 3600})
        self.assertIsNone(await ws._idle_policy_payload("worker_C"))

    @unittest_run_loop
    async def test_permissions_and_validation(self):
        await self.db.upsert_volunteer_device("volunteer_000111_abcd1234", "tok1", 111, "alice", "PC")
        url = "/api/node_settings/{}"
        body = {"idle_policy": "minutes", "idle_minutes": 30}
        # 未ログイン
        self.assertEqual((await self.client.put(url.format("worker_A"), json=body)).status, 401)
        # 一般ユーザーはクラスタを変更できない / 自分の端末は変更できる / 他人の端末は不可
        self.assertEqual((await self.client.put(url.format("worker_A"), json=body, cookies=self._cookie(111))).status, 403)
        self.assertEqual((await self.client.put(url.format("volunteer_000111_abcd1234"), json=body, cookies=self._cookie(111))).status, 200)
        self.assertEqual((await self.client.put(url.format("volunteer_000111_abcd1234"), json=body, cookies=self._cookie(222))).status, 403)
        # 管理者はクラスタを変更できる、範囲外は 400
        admin = self._cookie(999, admin=True)
        self.assertEqual((await self.client.put(url.format("worker_A"), json=body, cookies=admin)).status, 200)
        bad = {"idle_policy": "minutes", "idle_minutes": 1000}
        self.assertEqual((await self.client.put(url.format("worker_A"), json=bad, cookies=admin)).status, 400)
        self.assertEqual((await self.client.put(url.format("worker_A"), json={"idle_policy": "x"}, cookies=admin)).status, 400)
        # 一覧: 一般ユーザーは自分の端末だけ
        listing = await (await self.client.get("/api/node_settings", cookies=self._cookie(111))).json()
        self.assertEqual(list(listing["settings"].keys()), ["volunteer_000111_abcd1234"])
        self.assertEqual(await self.web_server._idle_policy_payload("worker_A"), {"enabled": True, "timeout_sec": 1800})

    @unittest_run_loop
    async def test_volunteer_receives_policy_in_acks(self):
        _, token = await self.db.create_volunteer_token(111222333444, "alice")
        await self.db.upsert_worker_settings("volunteer_333444_abcdef01", "never", 15, 111222333444)
        ws = await self.client.ws_connect(f"/api/worker/ws?token={token}")
        await ws.send_json({"type": "register", "node_name": "PC", "device_id": "abcdef0123456789", "supported_engines": ["voicevox"]})
        ack = await ws.receive_json()
        self.assertEqual(ack.get("idle_policy"), {"enabled": False})
        await ws.send_json({"type": "heartbeat", "active_tasks": 0})
        hb = await ws.receive_json()
        self.assertEqual(hb.get("idle_policy"), {"enabled": False})
        await ws.close()


class TestAgentAuthHeaders(unittest.TestCase):
    def test_header_follows_current_secret(self):
        cfg = {"s": "first"}
        worker_auth.configure_agent_auth(lambda: cfg["s"])
        self.assertEqual(worker_auth.agent_auth_headers(), {"X-Worker-Token": "first"})
        cfg["s"] = "rotated"
        self.assertEqual(worker_auth.agent_auth_headers({"A": 1}), {"A": 1, "X-Worker-Token": "rotated"})
        cfg["s"] = ""
        self.assertEqual(worker_auth.agent_auth_headers(), {})


def _load_agent(token: str):
    with patch.dict(os.environ, {"WORKER_SHARED_SECRET": token}), patch.object(sys, "argv", ["agent.py"]):
        path = os.path.join(os.path.dirname(__file__), "..", "src", "web", "static", "agent.py")
        spec = importlib.util.spec_from_file_location(f"agent_under_test_{token or 'none'}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


class _AgentAppBase(AioHTTPTestCase):
    TOKEN = "agent_secret_y"

    async def get_application(self):
        self.agent = _load_agent(self.TOKEN)

        async def ok(request):
            return web.json_response({"ok": True})
        app = web.Application(middlewares=[self.agent.agent_auth_middleware])
        app.router.add_get("/health", ok)
        app.router.add_post("/engine/start", ok)
        app.router.add_get("/status", ok)
        return app


class TestAgentAuthMiddleware(_AgentAppBase):
    @unittest_run_loop
    async def test_rules(self):
        self.assertEqual((await self.client.get("/health")).status, 200)
        self.assertEqual((await self.client.post("/engine/start")).status, 401)
        self.assertEqual((await self.client.get("/status", headers={"X-Worker-Token": "wrong"})).status, 401)
        self.assertEqual((await self.client.post("/engine/start", headers={"X-Worker-Token": self.TOKEN})).status, 200)
        self.assertEqual((await self.client.get("/status", headers={"Authorization": f"Bearer {self.TOKEN}"})).status, 200)


class TestAgentAuthDisabledWithoutToken(_AgentAppBase):
    TOKEN = ""

    @unittest_run_loop
    async def test_no_token_configured_allows_lan_calls(self):
        self.assertEqual((await self.client.post("/engine/start")).status, 200)


if __name__ == "__main__":
    unittest.main()
