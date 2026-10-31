"""ボランティアの個人トークン: 発行・照合・失効、WS 接続時のボランティア/クラスタ判定と所有者の紐づけ"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock

import aiosqlite
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from src.config import AppConfig
from src.database import Database
from src.web.server import WebServer


class _Base(AioHTTPTestCase):
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

    def _cookie(self, user_id, name="alice", admin=False):
        token = self.web_server.auth.create_session_token({"id": user_id, "username": name, "global_name": name}, is_admin=admin)
        return {"ttsbot_session": token}


class TestVolunteerTokenStore(_Base):
    @unittest_run_loop
    async def test_issue_resolve_revoke(self):
        token_id, token = await self.db.create_volunteer_token(111222333444, "alice", "PC1")
        self.assertTrue(token.startswith("vt_"))
        owner = await self.db.resolve_volunteer_token(token)
        self.assertEqual(owner["discord_user_id"], 111222333444)
        self.assertEqual(owner["owner_name"], "alice")
        # トークン本体は DB に保存されない
        async with aiosqlite.connect(self.web_server.config.db_path) as c:
            rows = await (await c.execute("SELECT token_hash FROM volunteer_tokens")).fetchall()
        self.assertNotIn(token, [r[0] for r in rows])
        # 他人は失効できない、本人はできる
        self.assertFalse(await self.db.revoke_volunteer_token(token_id, 999))
        self.assertTrue(await self.db.revoke_volunteer_token(token_id, 111222333444))
        self.assertIsNone(await self.db.resolve_volunteer_token(token))
        self.assertIsNone(await self.db.resolve_volunteer_token("vt_unknown"))


class TestVolunteerWsClassification(_Base):
    async def _register(self, token, payload):
        ws = await self.client.ws_connect(f"/api/worker/ws?token={token}")
        await ws.send_json(dict({"type": "register", "supported_engines": ["voicevox"], "capacity": 2}, **payload))
        ack = await ws.receive_json()
        return ws, ack

    @unittest_run_loop
    async def test_personal_token_is_volunteer_with_owner(self):
        _, token = await self.db.create_volunteer_token(111222333444, "alice", "PC1")
        ws, ack = await self._register(token, {"node_id": "volunteer_random_1234", "node_name": "GamingPC",
                                               "device_id": "abcdef0123456789"})
        self.assertEqual(ack["node_id"], "volunteer_333444_abcdef01")
        node = self.web_server.tts_manager.register_ws_worker_node.call_args[0][0]
        self.assertTrue(node.is_volunteer)
        self.assertEqual(node.owner_name, "alice")
        d = node.to_dict()
        self.assertTrue(d["is_volunteer"])
        self.assertEqual(d["owner_name"], "alice")
        devices = await self.db.list_volunteer_devices(111222333444)
        self.assertEqual([x["node_id"] for x in devices], ["volunteer_333444_abcdef01"])
        await ws.close()

    @unittest_run_loop
    async def test_cluster_tunnel_is_not_volunteer(self):
        ws, ack = await self._register("shared_secret_x", {"node_id": "worker_MasterUbuntu", "node_name": "Worker (MasterUbuntu)"})
        node = self.web_server.tts_manager.register_ws_worker_node.call_args[0][0]
        self.assertFalse(node.is_volunteer)
        self.assertFalse(node.to_dict()["is_volunteer"])
        self.assertNotIn("Volunteer-", node.machine_name)
        await ws.close()

    @unittest_run_loop
    async def test_revoked_or_unknown_token_rejected(self):
        token_id, token = await self.db.create_volunteer_token(1, "bob")
        await self.db.revoke_volunteer_token(token_id)
        for t in (token, "vt_unknown"):
            resp = await self.client.get(f"/api/worker/ws?token={t}")
            self.assertEqual(resp.status, 401)


class TestVolunteerTokenApi(_Base):
    @unittest_run_loop
    async def test_issue_list_revoke_via_api(self):
        resp = await self.client.post("/api/volunteer/tokens", json={"label": "PC1"})
        self.assertEqual(resp.status, 401)  # 未ログイン

        alice = self._cookie(111, "alice")
        resp = await self.client.post("/api/volunteer/tokens", json={"label": "PC1"}, cookies=alice)
        data = await resp.json()
        self.assertEqual(resp.status, 200)
        self.assertTrue(data["token"].startswith("vt_"))

        listing = await (await self.client.get("/api/volunteer/tokens", cookies=alice)).json()
        self.assertEqual([t["token_id"] for t in listing["tokens"]], [data["token_id"]])
        self.assertNotIn("token_hash", listing["tokens"][0])

        # 他人は失効できない
        bob = self._cookie(222, "bob")
        resp = await self.client.delete(f"/api/volunteer/tokens/{data['token_id']}", cookies=bob)
        self.assertEqual(resp.status, 404)
        resp = await self.client.delete(f"/api/volunteer/tokens/{data['token_id']}", cookies=alice)
        self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
