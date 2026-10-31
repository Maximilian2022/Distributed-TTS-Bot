"""クラスタワーカーおよびボランティアワーカーの所有者 (Owner) 紐づけ・WebUI 設定 API のテスト"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock

import aiosqlite
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from src.config import AppConfig
from src.database import Database
from src.tts.node_pool import TTSNode, TTSNodeConfig
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
        for pool_name in ("voicevox_pool", "sharevox_pool", "coeiroink_pool"):
            pool_mock = MagicMock()
            pool_mock.nodes = []
            pool_mock.get_status.return_value = []
            setattr(tts, pool_name, pool_mock)

        self.web_server = WebServer(
            bot=MagicMock(),
            config=config,
            database=self.db,
            cache_manager=MagicMock(),
            tts_manager=tts
        )
        return self.web_server.app

    async def tearDownAsync(self):
        await self.db.close()
        self.tmp.cleanup()

    def _cookie(self, user_id, name="alice", admin=False):
        token = self.web_server.auth.create_session_token(
            {"id": user_id, "username": name, "global_name": name},
            is_admin=admin
        )
        return {"ttsbot_session": token}


class TestWorkerOwnerStorage(_Base):
    @unittest_run_loop
    async def test_owner_name_in_worker_settings(self):
        # 1. 初期upsertでowner_nameとowner_user_idを保存
        await self.db.upsert_worker_settings("worker_OMEN", "never", 15, 111, owner_name="Maximilian", owner_user_id=310034034690359308)
        settings = await self.db.get_all_worker_settings()
        self.assertIn("worker_OMEN", settings)
        self.assertEqual(settings["worker_OMEN"]["owner_name"], "Maximilian")
        self.assertEqual(settings["worker_OMEN"]["owner_user_id"], 310034034690359308)
        self.assertEqual(settings["worker_OMEN"]["idle_policy"], "never")

        # 2. update_worker_owner で所有者名・ID更新
        await self.db.update_worker_owner("worker_OMEN", "チェインドベル", 222, owner_user_id=310034034690359308)
        settings = await self.db.get_all_worker_settings()
        self.assertEqual(settings["worker_OMEN"]["owner_name"], "チェインドベル")
        self.assertEqual(settings["worker_OMEN"]["owner_user_id"], 310034034690359308)
        # idle_policy が保持されていること
        self.assertEqual(settings["worker_OMEN"]["idle_policy"], "never")

        # 3. 新規ノードを update_worker_owner で直接設定
        await self.db.update_worker_owner("worker_NEW", "Alice", 333, owner_user_id=123456)
        settings = await self.db.get_all_worker_settings()
        self.assertEqual(settings["worker_NEW"]["owner_name"], "Alice")
        self.assertEqual(settings["worker_NEW"]["owner_user_id"], 123456)


class TestWorkerOwnerApi(_Base):
    @unittest_run_loop
    async def test_admin_can_update_cluster_worker_owner(self):
        admin_cookie = self._cookie(1, "admin_user", admin=True)
        resp = await self.client.put(
            "/api/node_settings/worker_MASTEROMEN",
            json={"owner_name": "Maximilian"},
            cookies=admin_cookie
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["owner_name"], "Maximilian")

        # DBに保存されていることを確認
        settings = await self.db.get_all_worker_settings()
        self.assertEqual(settings["worker_MASTEROMEN"]["owner_name"], "Maximilian")

    @unittest_run_loop
    async def test_non_admin_cannot_update_cluster_worker_owner(self):
        user_cookie = self._cookie(2, "normal_user", admin=False)
        resp = await self.client.put(
            "/api/node_settings/worker_MASTEROMEN",
            json={"owner_name": "Hacker"},
            cookies=user_cookie
        )
        self.assertEqual(resp.status, 403)

    @unittest_run_loop
    async def test_volunteer_owner_can_update_own_device(self):
        # ボランティア端末を作成
        token_id, _ = await self.db.create_volunteer_token(12345, "bob")
        await self.db.upsert_volunteer_device("volunteer_device_1", token_id, 12345, "bob", "BobPC")

        # Bob本人として更新
        bob_cookie = self._cookie(12345, "bob", admin=False)
        resp = await self.client.put(
            "/api/node_settings/volunteer_device_1",
            json={"owner_name": "Bob (Home)"},
            cookies=bob_cookie
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["owner_name"], "Bob (Home)")

        # 他人は更新不可
        alice_cookie = self._cookie(99999, "alice", admin=False)
        resp = await self.client.put(
            "/api/node_settings/volunteer_device_1",
            json={"owner_name": "Alice Stole It"},
            cookies=alice_cookie
        )
        self.assertEqual(resp.status, 403)

    @unittest_run_loop
    async def test_api_worker_nodes_returns_owner_name(self):
        # DBに所有者情報をセット
        await self.db.update_worker_owner("worker_OMEN", "Maximilian", 1)

        # TTSNode を模擬
        cfg = TTSNodeConfig(url="http://192.168.1.100:50021", priority=1, max_concurrent=2)
        node = TTSNode(cfg, hostname="OMEN")
        node.id = "worker_OMEN_voicevox"
        self.web_server.tts_manager.voicevox_pool.get_status.return_value = [node.to_dict()]

        # ログインしてノード一覧取得
        admin_cookie = self._cookie(1, "admin_user", admin=True)
        resp = await self.client.get("/api/worker/nodes", cookies=admin_cookie)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        nodes = data["pools"]["voicevox"]
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["owner_name"], "Maximilian")


class TestManagerNodeOwnerApi(_Base):
    @unittest_run_loop
    async def test_admin_can_update_master_and_slave_owner(self):
        admin_cookie = self._cookie(1, "admin_user", admin=True)

        # 待機ノードを1台模擬登録
        self.web_server._record_ha_slave_heartbeat(
            node_id="slave_debian_01",
            hostname="debian-host",
            ip="192.168.1.248",
            is_sync=True,
            owner_name="InitialOwner"
        )

        # 1. 主系 (Master) の所有者を更新
        resp = await self.client.post(
            "/api/system/manager_nodes/owner",
            json={"node_id": "master_primary", "owner_name": "HostAdmin (Maximilian)"},
            cookies=admin_cookie
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["owner_name"], "HostAdmin (Maximilian)")

        # 2. 待機系 (Slave) の所有者を更新
        resp = await self.client.post(
            "/api/system/manager_nodes/owner",
            json={"node_id": "slave_debian_01", "owner_name": "DebianSubAdmin"},
            cookies=admin_cookie
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["owner_name"], "DebianSubAdmin")

        # 3. GET /api/system/manager_nodes で両方の所有者が返却されることを確認
        resp = await self.client.get("/api/system/manager_nodes", cookies=admin_cookie)
        self.assertEqual(resp.status, 200)
        mn_data = await resp.json()
        self.assertEqual(mn_data["master"]["owner_name"], "HostAdmin (Maximilian)")
        self.assertEqual(len(mn_data["slaves"]), 1)
        self.assertEqual(mn_data["slaves"][0]["owner_name"], "DebianSubAdmin")

    @unittest_run_loop
    async def test_non_admin_cannot_update_manager_node_owner(self):
        user_cookie = self._cookie(2, "normal_user", admin=False)
        resp = await self.client.post(
            "/api/system/manager_nodes/owner",
            json={"node_id": "master_primary", "owner_name": "Attacker"},
            cookies=user_cookie
        )
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()

