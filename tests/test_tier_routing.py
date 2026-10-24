import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from src.tts import node_pool

class TestTierRouting(unittest.TestCase):
    def test_tier_routing_defaults(self):
        # デフォルトの4段階Tierルーティングが取得できること
        default_order = node_pool.get_tier_routing_order("default")
        self.assertEqual(len(default_order), 4)
        self.assertEqual(set(default_order), {"gpu_ram", "cpu_ram", "gpu_ssd", "cpu_ssd"})

    def test_tier_routing_set_and_get(self):
        new_order = ["gpu_ssd", "gpu_ram", "cpu_ssd", "cpu_ram"]
        self.assertTrue(node_pool.set_tier_routing_order("voicevox", new_order))

        # 取得確認（大文字小文字の非依存性含む）
        self.assertEqual(node_pool.get_tier_routing_order("voicevox"), new_order)
        self.assertEqual(node_pool.get_tier_routing_order("VOICEVOX"), new_order)

        # 設定されていないエンジンはdefaultへフォールバック
        self.assertEqual(node_pool.get_tier_routing_order("unknown_engine"), node_pool.get_tier_routing_order("default"))

    def test_tier_routing_invalid_orders(self):
        # 要素数不足
        self.assertFalse(node_pool.set_tier_routing_order("coeiroink", ["gpu_ram", "cpu_ram"]))

        # 不正なTierキー
        self.assertFalse(node_pool.set_tier_routing_order("coeiroink", ["gpu_ram", "cpu_ram", "invalid_tier", "cpu_ssd"]))

        # 重複あり（4要素だがsetにしたとき要素数不足）
        self.assertFalse(node_pool.set_tier_routing_order("coeiroink", ["gpu_ram", "gpu_ram", "cpu_ram", "cpu_ssd"]))

    def test_tier_routing_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_json = Path(tmpdir) / "test_tier_routing.json"
            with patch.object(node_pool, "TIER_ROUTING_CONFIG_PATH", test_json):
                custom_orders = {
                    "default": ["cpu_ram", "gpu_ram", "cpu_ssd", "gpu_ssd"],
                    "sharevox": ["gpu_ssd", "cpu_ssd", "gpu_ram", "cpu_ram"]
                }

                # 保存
                self.assertTrue(node_pool.save_tier_routing_to_disk(custom_orders))
                self.assertTrue(test_json.exists())

                # 一旦リセット
                node_pool.set_tier_routing_order("default", ["gpu_ram", "cpu_ram", "gpu_ssd", "cpu_ssd"])

                # ロードして復元されることを確認
                node_pool.load_tier_routing_from_disk()
                self.assertEqual(node_pool.get_tier_routing_order("default"), custom_orders["default"])
                self.assertEqual(node_pool.get_tier_routing_order("sharevox"), custom_orders["sharevox"])


class TestTTSNodeColdStartAlert(unittest.TestCase):
    def test_sleep_cold_start_alert_suppression(self):
        """スリープ停止中または起動初期化中のポート切断エラーでは管理者DMアラートが抑止されること"""
        from unittest.mock import patch, MagicMock
        from src.config import TTSNodeConfig
        from src.tts.node_pool import TTSNode

        conf = TTSNodeConfig(id="worker_test_coeiroink", url="http://192.168.1.100:50031", enabled=True)
        node = TTSNode(conf, specs={"engine_running": False})
        node.engine_running = False

        with patch("src.utils.alert_service.alert_service.notify") as mock_notify:
            # 停止中のポート切断エラー
            node.mark_failed(cooldown_seconds=10.0, reason="COEIROINK network error: Server disconnected")
            self.assertFalse(node.is_healthy)
            # アラート通知が抑止されたこと
            mock_notify.assert_not_called()

        with patch("src.utils.alert_service.alert_service.notify") as mock_notify:
            # 致命的な未知のエラー (HTTP 500 等) では通常通り通知されること
            node.is_healthy = True
            node.unhealthy_until = 0.0
            node.mark_failed(cooldown_seconds=10.0, reason="Internal Server Error HTTP 500")
            mock_notify.assert_called_once()

    def test_heartbeat_does_not_revive_offline_engine(self):
        """エンジンが停止中 (False) の場合、ハートビートを受信しても is_healthy が True に誤復帰しないこと"""
        from src.config import TTSNodeConfig
        from src.tts.node_pool import TTSNode

        conf = TTSNodeConfig(id="worker_test_coeiroink", url="http://192.168.1.100:50031", enabled=True)
        node = TTSNode(conf, specs={"engine_running": False})
        node.engine_running = False
        node.is_healthy = False
        node.unhealthy_until = 0.0  # クールダウン期限経過済み

        # ハートビート受信
        node.update_heartbeat()
        # エンジンが停止中のため、is_healthy は False のまま維持されること
        self.assertFalse(node.is_healthy)

        # エンジンが稼働中 (True) に変わった場合
        node.engine_running = True
        node.specs["engine_running"] = True
        node.update_heartbeat()
        # 正常に復帰すること
        self.assertTrue(node.is_healthy)


if __name__ == "__main__":
    unittest.main()
