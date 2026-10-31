"""ノードセットアップ用トークン (Setup Token) による所有者安全紐づけ機能のテスト"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock

import aiosqlite
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from src.config import AppConfig
from src.database import Database, CLUSTER_WORKER_TOKEN_PREFIX, STANDBY_HA_TOKEN_PREFIX
from src.tts.node_pool import TTSNode, TTSNodeConfig
from src.web.server import WebServer


class TestNodeSetupTokenDB(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "bot.db")
        self.db = Database(self.db_path)
        await self.db.init_db()

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def test_get_or_create_and_resolve(self):
        user_id = 123456789
        owner_name = "Alice"

        # 1. 初回生成
        token1 = await self.db.get_or_create_node_setup_token(user_id, owner_name, "worker")
        self.assertTrue(token1.startswith(CLUSTER_WORKER_TOKEN_PREFIX))

        # 2. 再度呼び出すと同一の有効トークンを返す (安定性)
        token2 = await self.db.get_or_create_node_setup_token(user_id, owner_name, "worker")
        self.assertEqual(token1, token2)

        # 3. トークン照合 (resolve)
        resolved = await self.db.resolve_node_setup_token(token1, "worker")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["discord_user_id"], user_id)
        self.assertEqual(resolved["owner_name"], owner_name)
        self.assertEqual(resolved["token_type"], "worker")

        # 4. 別タイプの照合 (standby で照合すると None)
        self.assertIsNone(await self.db.resolve_node_setup_token(token1, "standby"))

        # 5. 失効 (revoke)
        count = await self.db.revoke_node_setup_tokens(user_id, "worker")
        self.assertEqual(count, 1)

        # 6. 失効後は None
        self.assertIsNone(await self.db.resolve_node_setup_token(token1, "worker"))

        # 7. 失効後に再度取得すると新しいトークンが生成される
        token3 = await self.db.get_or_create_node_setup_token(user_id, owner_name, "worker")
        self.assertNotEqual(token1, token3)


class TestNodeSetupTokenWeb(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = AppConfig()
        config.worker_shared_secret = "master_shared_secret_xxx"
        config.ha_secret = "master_ha_secret_yyy"
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

    @unittest_run_loop
    async def test_admin_setup_tokens_api(self):
        # 1. 未ログイン: 401
        res = await self.client.get("/api/admin/setup_tokens")
        self.assertEqual(res.status, 401)

        # 2. 一般ユーザー: 403
        res = await self.client.get("/api/admin/setup_tokens", cookies=self._cookie(111, admin=False))
        self.assertEqual(res.status, 403)

        # 3. 管理者: 200 OK
        admin_id = 999000111
        res = await self.client.get("/api/admin/setup_tokens", cookies=self._cookie(admin_id, name="AdminUser", admin=True))
        self.assertEqual(res.status, 200)
        data = await res.json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["worker_token"].startswith(CLUSTER_WORKER_TOKEN_PREFIX))
        self.assertTrue(data["standby_token"].startswith(STANDBY_HA_TOKEN_PREFIX))
        self.assertEqual(data["owner_user_id"], admin_id)
        self.assertEqual(data["owner_name"], "AdminUser")

        # 4. トークンローテーション (再発行)
        old_worker_tok = data["worker_token"]
        res_rot = await self.client.post("/api/admin/setup_tokens/rotate", cookies=self._cookie(admin_id, name="AdminUser", admin=True))
        self.assertEqual(res_rot.status, 200)
        rot_data = await res_rot.json()
        self.assertNotEqual(rot_data["worker_token"], old_worker_tok)

        # 旧トークンは失効している
        self.assertIsNone(await self.db.resolve_node_setup_token(old_worker_tok, "worker"))

    @unittest_run_loop
    async def test_worker_register_with_setup_token(self):
        # 管理者 Charlie が Setup Token を発行
        charlie_id = 333444555
        token = await self.db.get_or_create_node_setup_token(charlie_id, "Charlie", "worker")

        # Worker が Setup Token を Authorization ヘッダーに付与して Master に自動登録
        payload = {
            "node_id": "worker_gpu_charlie",
            "node_name": "Charlie-Rig",
            "ip": "192.168.1.55",
            "agent_port": 50030,
            "specs": {
                "hostname": "Charlie-Rig",
                "gpu": True,
                "gpu_name": "RTX 4090"
            },
            "engines": {
                "voicevox": {"port": 50021, "running": True}
            }
        }
        headers = {"Authorization": f"Bearer {token}"}
        res = await self.client.post("/api/worker/register", json=payload, headers=headers)
        self.assertEqual(res.status, 200)

        # 1. self.web_server.cluster_workers に Charlie の所有者情報が紐づいている
        self.assertIn("worker_gpu_charlie", self.web_server.cluster_workers)
        cw = self.web_server.cluster_workers["worker_gpu_charlie"]
        self.assertEqual(cw["owner_name"], "Charlie")
        self.assertEqual(cw["owner_user_id"], charlie_id)

        # 2. DB の worker_settings にも永続化されている
        settings = await self.db.get_all_worker_settings()
        self.assertIn("worker_gpu_charlie", settings)
        self.assertEqual(settings["worker_gpu_charlie"]["owner_user_id"], charlie_id)
        self.assertEqual(settings["worker_gpu_charlie"]["owner_name"], "Charlie")

    @unittest_run_loop
    async def test_ha_slave_status_with_setup_token(self):
        # 管理者 Dave が Standby Setup Token を発行
        dave_id = 777888999
        token = await self.db.get_or_create_node_setup_token(dave_id, "Dave", "standby")

        # Standby Slave が Setup Token を X-HA-Secret に付与してステータス通知
        headers = {
            "X-HA-Secret": token,
            "X-HA-Node-ID": "slave_dave_box",
            "X-HA-Hostname": "DaveStandbyServer",
            "X-HA-Local-IP": "192.168.1.77"
        }
        res = await self.client.get("/api/ha/status", headers=headers)
        self.assertEqual(res.status, 200)

        # 1. self.web_server.ha_slaves に Dave の所有者情報が紐づいている
        self.assertIn("slave_dave_box", self.web_server.ha_slaves)
        slave = self.web_server.ha_slaves["slave_dave_box"]
        self.assertEqual(slave["owner_name"], "Dave")
        self.assertEqual(slave["owner_user_id"], dave_id)

    @unittest_run_loop
    async def test_protection_against_host_env_overwrite(self):
        # 状況:
        # Dave さんが自分の Setup Token で登録したクラスタワーカーがある
        dave_id = 777888999
        await self.db.upsert_worker_settings("worker_dave", "default", 15, dave_id, owner_name="Dave", owner_user_id=dave_id)
        self.web_server.cluster_workers["worker_dave"] = {
            "node_id": "worker_dave",
            "owner_name": "Dave",
            "owner_user_id": dave_id
        }

        # ホスト管理者 (Alice, ID=111) が .env を設定して自動バインドを実行
        self.web_server.config.discord_owner_user_id = 111
        await self.web_server._auto_bind_host_nodes_owner(
            {"id": 111, "username": "Alice"},
            is_admin=True
        )

        # Master ノードはホストの Alice にバインドされる
        master_st = (await self.db.get_all_worker_settings()).get("master_primary", {})
        self.assertEqual(master_st.get("owner_user_id"), 111)

        # しかし Dave さんのクラスタワーカーは上書きされず、Dave さんのまま保護される！
        dave_st = (await self.db.get_all_worker_settings()).get("worker_dave", {})
        self.assertEqual(dave_st.get("owner_user_id"), dave_id)
        self.assertEqual(dave_st.get("owner_name"), "Dave")
        self.assertEqual(self.web_server.cluster_workers["worker_dave"]["owner_user_id"], dave_id)


if __name__ == "__main__":
    unittest.main()

