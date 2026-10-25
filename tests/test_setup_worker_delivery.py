import unittest
from pathlib import Path
from unittest.mock import MagicMock
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from src.web.server import WebServer
from src.config import AppConfig


class TestSetupWorkerDelivery(AioHTTPTestCase):
    async def get_application(self):
        config = AppConfig()
        config.worker_shared_secret = "test_secret_12345"
        config.web_dashboard_url = "https://ttsbot.example.com"

        bot = MagicMock()
        bot.is_ready.return_value = True
        bot.latency = 0.02
        bot.guilds = []

        db = MagicMock()
        cache_manager = MagicMock()
        tts_manager = MagicMock()

        self.web_server = WebServer(
            bot=bot,
            config=config,
            database=db,
            cache_manager=cache_manager,
            tts_manager=tts_manager
        )
        return self.web_server.app

    @unittest_run_loop
    async def test_setup_worker_sh_delivery_wan(self):
        """WAN経由アクセス時 (CF-Connecting-IPあり): web_dashboard_url が注入されるか"""
        headers = {
            "CF-Connecting-IP": "203.0.113.195",
            "X-Forwarded-Proto": "https"
        }
        resp = await self.client.get("/setup-worker.sh", headers=headers)
        self.assertEqual(resp.status, 200)
        text = await resp.text()

        self.assertNotIn('MASTER_URL="${TTSBOT_MASTER_URL:-http://localhost:8088}"', text)
        self.assertNotIn('MASTER_URL="${TTSBOT_MASTER_URL:-http://localhost:8080}"', text)
        self.assertIn('MASTER_URL="${TTSBOT_MASTER_URL:-https://ttsbot.example.com}"', text)
        self.assertIn('WORKER_SECRET="${TTSBOT_WORKER_SECRET:-test_secret_12345}"', text)

    @unittest_run_loop
    async def test_setup_worker_sh_delivery_lan(self):
        """宅内LANアクセス時 (プライベートIP直接): リクエスト先Hostヘッダーが優先注入されるか"""
        headers = {
            "Host": "192.168.11.248:8088"
        }
        resp = await self.client.get("/setup-worker.sh", headers=headers)
        self.assertEqual(resp.status, 200)
        text = await resp.text()

        # LAN内の直接通信URLが注入されていること
        self.assertIn('MASTER_URL="${TTSBOT_MASTER_URL:-http://192.168.11.248:8088}"', text)
        self.assertIn('WORKER_SECRET="${TTSBOT_WORKER_SECRET:-test_secret_12345}"', text)

    @unittest_run_loop
    async def test_setup_worker_sh_static_alias_route(self):
        """/static/setup-worker.sh でも動的注入が行われるか"""
        headers = {
            "CF-Connecting-IP": "203.0.113.195",
            "X-Forwarded-Proto": "https"
        }
        resp = await self.client.get("/static/setup-worker.sh", headers=headers)
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn('MASTER_URL="${TTSBOT_MASTER_URL:-https://ttsbot.example.com}"', text)

    @unittest_run_loop
    async def test_setup_worker_ps1_delivery_wan_and_lan(self):
        """Windows用 setup-worker.ps1 で WAN/LAN に応じたURLとシークレットが注入されるか"""
        # WAN
        wan_resp = await self.client.get("/setup-worker.ps1", headers={"CF-Connecting-IP": "203.0.113.195"})
        self.assertEqual(wan_resp.status, 200)
        wan_text = await wan_resp.text()
        self.assertIn('"https://ttsbot.example.com"', wan_text)
        self.assertIn('"test_secret_12345"', wan_text)

        # LAN
        lan_resp = await self.client.get("/setup-worker.ps1", headers={"Host": "192.168.11.248:8088"})
        self.assertEqual(lan_resp.status, 200)
        lan_text = await lan_resp.text()
        self.assertIn('"http://192.168.11.248:8088"', lan_text)
        self.assertIn('"test_secret_12345"', lan_text)

    @unittest_run_loop
    async def test_static_agent_py_delivery(self):
        """/static/agent.py が正常に配信されるか"""
        resp = await self.client.get("/static/agent.py")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn("AGENT_VERSION", text)
        self.assertIn("class BufferLogHandler", text)

    @unittest_run_loop
    async def test_ws_duplicate_registration_skipped_when_http_node_active(self):
        """常設HTTPクラスタノードが存在する場合、WSボランティアノードとしての二重登録が抑止されるか"""
        # 既存のHTTPノードをモックプールに配備
        http_node = MagicMock()
        http_node.id = "worker_MasterDebian_voicevox"
        http_node.is_ws = False

        self.web_server.tts_manager.voicevox_pool.nodes = [http_node]
        self.web_server.tts_manager.sharevox_pool.nodes = []
        self.web_server.tts_manager.coeiroink_pool.nodes = []

        # WebSocket 接続して register メッセージを送信
        ws_client = await self.client.ws_connect("/api/worker/ws?token=test_secret_12345")
        await ws_client.send_json({
            "type": "register",
            "node_id": "worker_MasterDebian",
            "node_name": "Worker (MasterDebian)",
            "supported_engines": ["voicevox"],
            "capacity": 2
        })

        resp_data = await ws_client.receive_json()
        self.assertEqual(resp_data.get("type"), "register_ack")
        self.assertEqual(resp_data.get("status"), "ok")

        # register_ws_worker_node が呼ばれていないこと (二重登録抑止)
        self.web_server.tts_manager.register_ws_worker_node.assert_not_called()
        await ws_client.close()


if __name__ == "__main__":
    unittest.main()

