import unittest
from unittest.mock import patch, MagicMock, AsyncMock
import json
import os

from src.ha_watchdog import DockerSocketClient, HAWatchdog


class TestDockerSocketClient(unittest.TestCase):
    def setUp(self):
        self.client = DockerSocketClient("/tmp/fake_docker.sock")

    def test_find_tunnel_containers(self):
        mock_containers = [
            {"Id": "tunnel1234567890", "Names": ["/ttsbot-slave-tunnel"]},
            {"Id": "tunnel_standalone", "Names": ["/tunnel"]},
            {"Id": "app1234567890", "Names": ["/ttsbot-app"]},
            {"Id": "watchdog123456", "Names": ["/ttsbot-slave-standby-watchdog-1"]},
            {"Id": "worker123456", "Names": ["/ttsworker-agent"]},
            {"Id": "other_tunnel", "Names": ["/other_proxy_tunnel"]},
        ]
        with patch.object(self.client, "_send_request", return_value=(200, json.dumps(mock_containers).encode())):
            results = self.client.find_tunnel_containers()
            ids = [c["Id"] for c in results]
            self.assertIn("tunnel1234567890", ids)
            self.assertIn("tunnel_standalone", ids)
            self.assertNotIn("app1234567890", ids)
            self.assertNotIn("watchdog123456", ids)
            self.assertNotIn("worker123456", ids)

    def test_find_app_containers(self):
        mock_containers = [
            {"Id": "tunnel1234567890", "Names": ["/ttsbot-slave-tunnel"]},
            {"Id": "app1234567890", "Names": ["/ttsbot-app"]},
            {"Id": "watchdog123456", "Names": ["/ttsbot-slave-standby-watchdog-1"]},
        ]
        with patch.object(self.client, "_send_request", return_value=(200, json.dumps(mock_containers).encode())):
            results = self.client.find_app_containers()
            ids = [c["Id"] for c in results]
            self.assertIn("app1234567890", ids)
            self.assertNotIn("tunnel1234567890", ids)
            self.assertNotIn("watchdog123456", ids)


class TestHAWatchdogTunnelFailover(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, {
            "TTSBOT_MASTER_URL": "http://192.168.11.245:8088",
            "TTSBOT_HA_SECRET": "test_secret",
            "CLOUDFLARE_TUNNEL_TOKEN": "eyJhIjoidGVzdCJ9",
        })
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()

    async def test_promote_and_demote_with_tunnel(self):
        with patch("src.ha_watchdog.DockerSocketClient.is_available", return_value=False):
            watchdog = HAWatchdog()
            self.assertTrue(watchdog.has_tunnel)

            # 1. 昇格 (promote_to_active)
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))

            with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
                await watchdog.promote_to_active()
                self.assertEqual(watchdog.current_state, "active")

                # コマンド引数に app と tunnel の両方が含まれていること
                mock_exec.assert_called_once()
                args = mock_exec.call_args[0]
                self.assertIn("app", args)
                self.assertIn("tunnel", args)
                self.assertIn("--profile", args)
                self.assertIn("active", args)

            # 2. 降格 (demote_to_standby)
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
                await watchdog.demote_to_standby()
                self.assertEqual(watchdog.current_state, "standby")

                # コマンド引数に app と tunnel の両方が含まれていること
                mock_exec.assert_called_once()
                args = mock_exec.call_args[0]
                self.assertIn("app", args)
                self.assertIn("tunnel", args)
                self.assertIn("stop", args)

    async def test_promote_and_demote_without_tunnel(self):
        with patch.dict(os.environ, {"CLOUDFLARE_TUNNEL_TOKEN": "", "TUNNEL_TOKEN": ""}), \
             patch("src.ha_watchdog.DockerSocketClient.is_available", return_value=False):
            watchdog = HAWatchdog()
            self.assertFalse(watchdog.has_tunnel)

            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))

            # 1. 昇格 (promote_to_active)
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
                await watchdog.promote_to_active()
                self.assertEqual(watchdog.current_state, "active")

                # tunnel は含まれず app のみであること
                mock_exec.assert_called_once()
                args = mock_exec.call_args[0]
                self.assertIn("app", args)
                self.assertNotIn("tunnel", args)

            # 2. 降格 (demote_to_standby)
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
                await watchdog.demote_to_standby()
                self.assertEqual(watchdog.current_state, "standby")

                args = mock_exec.call_args[0]
                self.assertIn("app", args)
                self.assertNotIn("tunnel", args)

    async def test_socket_fallback_with_tunnel(self):
        """CLI コマンドが失敗した際の Unix Socket フォールバック動作テスト"""
        with patch.object(DockerSocketClient, "is_available", return_value=True):
            watchdog = HAWatchdog()
            self.assertTrue(watchdog.has_tunnel)

            # CLI を失敗させる
            with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("docker not found")), \
                 patch.object(watchdog.docker, "find_app_containers", return_value=[{"Id": "app_cid"}]), \
                 patch.object(watchdog.docker, "find_tunnel_containers", return_value=[{"Id": "tunnel_cid"}]), \
                 patch.object(watchdog.docker, "start_container", return_value=True) as mock_start, \
                 patch.object(watchdog.docker, "stop_container", return_value=True) as mock_stop:

                # 昇格
                await watchdog.promote_to_active()
                self.assertEqual(watchdog.current_state, "active")
                mock_start.assert_any_call("app_cid")
                mock_start.assert_any_call("tunnel_cid")

                # 降格
                await watchdog.demote_to_standby()
                self.assertEqual(watchdog.current_state, "standby")
                mock_stop.assert_any_call("app_cid", timeout_sec=5)
                mock_stop.assert_any_call("tunnel_cid", timeout_sec=5)



class TestHAWatchdogAutoUpdate(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, {
            "TTSBOT_MASTER_URL": "http://192.168.11.245:8088",
            "TTSBOT_HA_SECRET": "test_secret",
        })
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()

    def test_code_update_detection(self):
        with patch("src.ha_watchdog.DockerSocketClient.is_available", return_value=False):
            watchdog = HAWatchdog()
            watchdog.current_code_hash = "old_hash_12345"

            mock_response_data = {
                "status": "ok",
                "is_active": True,
                "code_hash": "new_hash_67890",
                "force_update": False,
            }

            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = json.dumps(mock_response_data).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp

            with patch("urllib.request.urlopen", return_value=mock_resp), \
                 patch.object(watchdog, "sync_code_snapshot") as mock_sync:
                is_healthy = watchdog.is_master_healthy()
                self.assertTrue(is_healthy)
                mock_sync.assert_called_once_with("new_hash_67890")

    def test_sync_code_snapshot(self):
        import io
        import tarfile
        import tempfile
        from pathlib import Path

        with patch("src.ha_watchdog.DockerSocketClient.is_available", return_value=False):
            watchdog = HAWatchdog()
            watchdog.current_code_hash = "old_hash"

            # テスト用 tarball の作成
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                sample_data = b"print('updated code')"
                ti = tarfile.TarInfo(name="src/sample_update.py")
                ti.size = len(sample_data)
                tar.addfile(ti, io.BytesIO(sample_data))
            tar_bytes = buf.getvalue()

            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = tar_bytes
            mock_resp.headers = {"X-Code-Hash": "new_hash"}
            mock_resp.__enter__.return_value = mock_resp

            with tempfile.TemporaryDirectory() as tmpdir:
                fake_src = Path(tmpdir) / "src"
                fake_src.mkdir()
                fake_file = fake_src / "ha_watchdog.py"
                fake_file.write_text("# fake watchdog")

                with patch.dict(os.environ, {"TTSBOT_SRC_DIR": str(fake_src)}), \
                     patch("urllib.request.urlopen", return_value=mock_resp), \
                     patch("os.execv") as mock_execv:

                    # 実行
                    success = watchdog.sync_code_snapshot("new_hash")
                    self.assertTrue(success)
                    self.assertEqual(watchdog.current_code_hash, "new_hash")
                    mock_execv.assert_called_once()
                    self.assertTrue((fake_src / "sample_update.py").exists())
                    self.assertEqual((fake_src / "sample_update.py").read_bytes(), b"print('updated code')")


if __name__ == "__main__":
    unittest.main()
