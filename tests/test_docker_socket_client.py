"""Unit tests for DockerSocketClient in ha_watchdog.py.

Verifies HTTP/1.0 protocol, chunked transfer decoding, app/tunnel container filtering,
and start/stop container behavior without requiring an active docker daemon.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from src.ha_watchdog import DockerSocketClient


class TestDockerSocketClient(unittest.TestCase):
    def setUp(self):
        self.client = DockerSocketClient(socket_path="/nonexistent/docker.sock")

    def test_dechunk_single_chunk(self):
        """単一チャンクの正常展開"""
        raw_chunked = b"d\r\nHello, World!\r\n0\r\n\r\n"
        result = DockerSocketClient._dechunk(raw_chunked)
        self.assertEqual(result, b"Hello, World!")

    def test_dechunk_multiple_chunks(self):
        """複数チャンクの正常結合"""
        # "Hello, " (7 bytes: 0x7), "Docker " (7 bytes: 0x7), "Socket!" (7 bytes: 0x7)
        raw_chunked = b"7\r\nHello, \r\n7\r\nDocker \r\n7\r\nSocket!\r\n0\r\n\r\n"
        result = DockerSocketClient._dechunk(raw_chunked)
        self.assertEqual(result, b"Hello, Docker Socket!")

    def test_dechunk_non_chunked_data(self):
        """chunked でないプレーンなデータはそのまま保持"""
        plain_data = b'[{"Id": "test12345", "Names": ["/ttsbot-app"]}]'
        result = DockerSocketClient._dechunk(plain_data)
        self.assertEqual(result, plain_data)

    def test_dechunk_empty_data(self):
        """空データ"""
        self.assertEqual(DockerSocketClient._dechunk(b""), b"")

    @patch("socket.socket")
    def test_send_request_http_10_and_chunked_response(self, mock_socket_cls):
        """HTTP/1.0 リクエスト送信と chunked レスポンス自動アンチャンクの検証"""
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock

        # Docker デーモンが chunked で返してきたケースをシミュレート
        body_content = json.dumps([{"Id": "abc12345", "Names": ["/ttsbot-slave-app-1"]}]).encode("utf-8")
        chunk_len_hex = f"{len(body_content):x}".encode("ascii")
        raw_response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            + chunk_len_hex + b"\r\n"
            + body_content + b"\r\n"
            b"0\r\n\r\n"
        )

        mock_sock.recv.side_effect = [raw_response, b""]

        status, body = self.client._send_request("GET", "/containers/json?all=1")

        self.assertEqual(status, 200)
        # HTTP/1.0 リクエストを送信しているか検証
        sent_req = mock_sock.sendall.call_args[0][0].decode("utf-8")
        self.assertIn("GET /containers/json?all=1 HTTP/1.0", sent_req)
        # chunked がアンチャンクされ、直接 JSON としてパースできるか検証
        parsed = json.loads(body.decode("utf-8"))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["Id"], "abc12345")

    @patch.object(DockerSocketClient, "_send_request")
    def test_find_app_containers_filtering(self, mock_send_req):
        """find_app_containers が正しく本体コンテナのみをフィルタリングするか検証"""
        containers_data = [
            {"Id": "c1", "Names": ["/ttsbot-slave-app-1"]},
            {"Id": "c2", "Names": ["/ttsbot-slave-standby-watchdog-1"]},
            {"Id": "c3", "Names": ["/ttsbot-slave-tunnel-1"]},
            {"Id": "c4", "Names": ["/ttsworker-agent"]},
            {"Id": "c5", "Names": ["/ttsbot-app"]},
            {"Id": "c6", "Names": ["/nginx-proxy"]},
        ]
        mock_send_req.return_value = (200, json.dumps(containers_data).encode("utf-8"))

        apps = self.client.find_app_containers()
        app_ids = [c["Id"] for c in apps]

        # c1 (ttsbot-slave-app-1) と c5 (ttsbot-app) のみがマッチし、watchdog/tunnel/agent/nginx は除外
        self.assertIn("c1", app_ids)
        self.assertIn("c5", app_ids)
        self.assertNotIn("c2", app_ids)
        self.assertNotIn("c3", app_ids)
        self.assertNotIn("c4", app_ids)
        self.assertNotIn("c6", app_ids)

    @patch.object(DockerSocketClient, "_send_request")
    def test_find_tunnel_containers_filtering(self, mock_send_req):
        """find_tunnel_containers が ttsbot 専用 tunnel のみを抽出し、mmo_cf_tunnel 等を除外するか検証"""
        containers_data = [
            {"Id": "c1", "Names": ["/ttsbot-slave-app-1"]},
            {"Id": "c2", "Names": ["/ttsbot-slave-tunnel"]},
            {"Id": "c3", "Names": ["/ttsbot-tunnel"]},
            {"Id": "c4", "Names": ["/ttsbot-slave-watchdog"]},
            {"Id": "c5", "Names": ["/mmo_cf_tunnel"]},
            {"Id": "c6", "Names": ["/discord-role-tunnel"]},
        ]
        mock_send_req.return_value = (200, json.dumps(containers_data).encode("utf-8"))

        tunnels = self.client.find_tunnel_containers()
        tunnel_ids = [c["Id"] for c in tunnels]

        self.assertIn("c2", tunnel_ids)
        self.assertIn("c3", tunnel_ids)
        self.assertNotIn("c1", tunnel_ids)
        self.assertNotIn("c4", tunnel_ids)
        self.assertNotIn("c5", tunnel_ids)  # mmo_cf_tunnel は除外されること
        self.assertNotIn("c6", tunnel_ids)  # discord-role-tunnel は除外されること

    @patch.object(DockerSocketClient, "_send_request")
    def test_start_container_status_codes(self, mock_send_req):
        """start_container のステータスコード判定 (204/304 で True、その他で False)"""
        mock_send_req.return_value = (204, b"")
        self.assertTrue(self.client.start_container("c123"))

        mock_send_req.return_value = (304, b"")
        self.assertTrue(self.client.start_container("c123"))

        mock_send_req.return_value = (404, b"no such container")
        self.assertFalse(self.client.start_container("c123"))

        mock_send_req.return_value = (500, b"internal server error")
        self.assertFalse(self.client.start_container("c123"))

    @patch.object(DockerSocketClient, "_send_request")
    def test_stop_container_status_codes(self, mock_send_req):
        """stop_container のステータスコード判定 (204/304 で True、その他で False)"""
        mock_send_req.return_value = (204, b"")
        self.assertTrue(self.client.stop_container("c123"))

        mock_send_req.return_value = (304, b"")
        self.assertTrue(self.client.stop_container("c123"))

        mock_send_req.return_value = (404, b"no such container")
        self.assertFalse(self.client.stop_container("c123"))


if __name__ == "__main__":
    unittest.main()

