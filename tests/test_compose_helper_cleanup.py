import unittest
from unittest.mock import patch

from src.web.static import agent as agent_mod


class TestComposeHelperCleanup(unittest.TestCase):
    def setUp(self):
        self.api = agent_mod.DockerAPI()

    def test_cleanup_stale_compose_helpers_scopes_strictly(self):
        """停止済み・期限切れの tts-compose-helper-* のみを削除し、実行中新規ヘルパーや他コンテナには触れないこと"""
        now = 2_000_000.0
        containers = [
            # 1) 旧命名・exited -> 削除対象
            {"Id": "h_exited", "Names": ["/tts-compose-helper-1999990"], "State": "exited", "Created": 1999990},
            # 2) 新命名・created (start失敗残骸) -> 削除対象
            {"Id": "h_created", "Names": ["/tts-compose-helper-1999995-a1b2"], "State": "created", "Created": 1999995},
            # 3) 10分超スタック中の running -> 削除対象
            {"Id": "h_stuck", "Names": ["/tts-compose-helper-1999000-ff00"], "State": "running", "Created": 1999000},
            # 4) 実行開始直後 (30秒前) の running -> 保護 (削除しない)
            {"Id": "h_active", "Names": ["/tts-compose-helper-1999970-cc11"], "State": "running", "Created": 1999970},
            # 5) 無関係な停止済みコンテナ -> 保護 (絶対に削除しない)
            {"Id": "other_stopped", "Names": ["/ttsworker-sharevox"], "State": "exited", "Created": 1900000},
            {"Id": "other_similar", "Names": ["/my-tts-compose-helper-123"], "State": "exited", "Created": 1900000},
        ]
        deleted_ids = []

        def fake_request(method, path, body=None, timeout=15):
            if method == "GET" and path == "/containers/json?all=1":
                return 200, containers
            if method == "DELETE":
                deleted_ids.append(path)
                return 204, ""
            return 404, ""

        with patch.object(self.api, "is_available", return_value=True), \
             patch.object(self.api, "request", side_effect=fake_request), \
             patch.object(agent_mod.time, "time", return_value=now):
            removed = self.api.cleanup_stale_compose_helpers(max_age_seconds=600)

        self.assertEqual(removed, 3)
        self.assertEqual(
            deleted_ids,
            [
                "/containers/h_exited?force=1&v=1",
                "/containers/h_created?force=1&v=1",
                "/containers/h_stuck?force=1&v=1",
            ],
        )

    def test_detach_true_sets_autoremove_and_cleans_on_start_failure(self):
        """detach=True では AutoRemove=True が設定され、万一 start 失敗時は即座に DELETE されること"""
        calls = []

        def fake_request_ok(method, path, body=None, timeout=15):
            calls.append((method, path, body))
            if method == "GET" and path == "/containers/json?all=1":
                return 200, []
            if method == "POST" and path.startswith("/containers/create?name="):
                return 201, {"Id": "cid_detach_ok"}
            if method == "POST" and path == "/containers/cid_detach_ok/start":
                return 204, ""
            return 200, {}

        with patch.object(self.api, "is_available", return_value=True), \
             patch.object(self.api, "get_host_work_dir", return_value="/work"), \
             patch.object(self.api, "ensure_image", return_value=True), \
             patch.object(self.api, "request", side_effect=fake_request_ok):
            code, _ = self.api.run_compose_via_helper("/work", "docker-compose.agent.yml", ["up", "-d"], detach=True)

        self.assertEqual(code, 0)
        create_calls = [c for c in calls if c[0] == "POST" and c[1].startswith("/containers/create?name=")]
        self.assertEqual(len(create_calls), 1)
        self.assertTrue(create_calls[0][2]["HostConfig"]["AutoRemove"])
        self.assertFalse(any(c[0] == "DELETE" for c in calls))

        # start 失敗時は残留しないよう DELETE されること
        calls.clear()

        def fake_request_fail_start(method, path, body=None, timeout=15):
            calls.append((method, path, body))
            if method == "GET" and path == "/containers/json?all=1":
                return 200, []
            if method == "POST" and path.startswith("/containers/create?name="):
                return 201, {"Id": "cid_detach_fail"}
            if method == "POST" and path == "/containers/cid_detach_fail/start":
                return 500, "boom"
            if method == "DELETE":
                return 204, ""
            return 200, {}

        with patch.object(self.api, "is_available", return_value=True), \
             patch.object(self.api, "get_host_work_dir", return_value="/work"), \
             patch.object(self.api, "ensure_image", return_value=True), \
             patch.object(self.api, "request", side_effect=fake_request_fail_start):
            code2, _ = self.api.run_compose_via_helper("/work", "docker-compose.agent.yml", ["up", "-d"], detach=True)

        self.assertEqual(code2, 1)
        self.assertIn(("DELETE", "/containers/cid_detach_fail?force=1&v=1", None), calls)

    def test_sync_mode_always_deletes_in_finally_even_on_wait_exception(self):
        """detach=False では AutoRemove=False でログ回収でき、wait 中に例外が起きても finally で必ず DELETE されること"""
        calls = []

        def fake_request_boom(method, path, body=None, timeout=15):
            calls.append((method, path, body))
            if method == "GET" and path == "/containers/json?all=1":
                return 200, []
            if method == "POST" and path.startswith("/containers/create?name="):
                return 201, {"Id": "cid_sync_boom"}
            if method == "POST" and path == "/containers/cid_sync_boom/start":
                return 204, ""
            if method == "POST" and path == "/containers/cid_sync_boom/wait":
                raise RuntimeError("socket timeout")
            if method == "DELETE":
                return 204, ""
            return 200, {}

        with patch.object(self.api, "is_available", return_value=True), \
             patch.object(self.api, "get_host_work_dir", return_value="C:\\Users\\worker\\ttsbot"), \
             patch.object(self.api, "ensure_image", return_value=True), \
             patch.object(self.api, "request", side_effect=fake_request_boom):
            with self.assertRaises(RuntimeError):
                self.api.run_compose_via_helper("/work", "docker-compose.worker.yml", ["up", "-d", "worker-voicevox"], detach=False)

        create_call = [c for c in calls if c[0] == "POST" and c[1].startswith("/containers/create?name=")][0]
        self.assertFalse(create_call[2]["HostConfig"]["AutoRemove"])
        self.assertIn(("DELETE", "/containers/cid_sync_boom?force=1&v=1", None), calls)


if __name__ == "__main__":
    unittest.main()
