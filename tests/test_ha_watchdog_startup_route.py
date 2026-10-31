"""HA Watchdog: 起動時の LAN/WAN 経路判定と、Master 再起動 (instance_id 変化) 時の LAN 直通検証"""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from src.ha_watchdog import HAWatchdog


class _Resp(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(json.dumps(payload).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(routes):
    """routes: {base_url: payload or Exception}。未登録の URL は接続不可"""
    def _open(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for base, result in routes.items():
            if url.startswith(base):
                if isinstance(result, Exception):
                    raise result
                return _Resp(result)
        raise urllib.error.URLError("unreachable")
    return _open


MASTER_OK = {"status": "ok", "role": "master", "master_instance_id": "master_new", "master_local_ips": ["192.168.1.10"],
             "master_port": 8088, "is_active": True}


class TestStartupRoute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {
            "TTSBOT_MASTER_URL": "https://tts.example.com",
            "TTSBOT_HA_SECRET": "secret",
            "TTSBOT_LOCAL_DB_PATH": os.path.join(self.tmp.name, "bot.db"),
        }

    def _watchdog(self, extra_env=None):
        env = dict(self.env, **(extra_env or {}))
        with patch.dict(os.environ, env, clear=False):
            return HAWatchdog()

    def test_lan_url_reachable_uses_lan(self):
        wd = self._watchdog({"TTSBOT_MASTER_LAN_URL": "http://192.168.1.10:8088"})
        with patch("urllib.request.urlopen", _fake_urlopen({"http://192.168.1.10:8088": MASTER_OK})):
            self.assertEqual(wd.probe_master_route_at_startup(), "lan")
        self.assertTrue(wd.is_direct_verified)
        self.assertEqual(wd.direct_master_url, "http://192.168.1.10:8088")

    def test_no_lan_candidate_falls_back_to_wan(self):
        wd = self._watchdog({"TTSBOT_MASTER_LAN_URL": "http://192.168.1.10:8088"})
        with patch("urllib.request.urlopen", _fake_urlopen({"https://tts.example.com": MASTER_OK})):
            self.assertEqual(wd.probe_master_route_at_startup(), "wan")
        self.assertFalse(wd.is_direct_verified)
        self.assertEqual(wd.direct_master_url, "")

    def test_private_master_url_is_tried_as_lan(self):
        wd = self._watchdog({"TTSBOT_MASTER_URL": "http://192.168.1.10:8088"})
        with patch("urllib.request.urlopen", _fake_urlopen({"http://192.168.1.10:8088": MASTER_OK})):
            self.assertEqual(wd.probe_master_route_at_startup(), "lan")

    def test_non_master_response_is_rejected(self):
        wd = self._watchdog({"TTSBOT_MASTER_LAN_URL": "http://192.168.1.10:8088"})
        not_master = dict(MASTER_OK, role="slave")
        with patch("urllib.request.urlopen", _fake_urlopen({"http://192.168.1.10:8088": not_master})):
            self.assertEqual(wd.probe_master_route_at_startup(), "wan")


class TestMasterRestartWithWanDown(unittest.TestCase):
    """Master 再起動 (instance_id 変化) と WAN 断が重なっても、LAN で生存確認でき誤昇格しないこと"""

    def test_lan_handshake_accepts_new_instance_id(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {"TTSBOT_MASTER_URL": "https://tts.example.com", "TTSBOT_HA_SECRET": "secret",
               "TTSBOT_LOCAL_DB_PATH": os.path.join(tmp.name, "bot.db")}
        with patch.dict(os.environ, env, clear=False):
            wd = HAWatchdog()
        # 前回キャッシュ: 旧 instance_id と Master のローカル IP
        wd.cached_master_local_ips = ["192.168.1.10"]
        wd.cached_master_port = 8088
        wd.master_instance_id = "master_old"
        wd.is_direct_verified = False
        wd.last_direct_check_time = 0.0
        routes = {"https://tts.example.com": urllib.error.URLError("tunnel down"),
                  "http://192.168.1.10:8088": MASTER_OK}
        with patch("urllib.request.urlopen", _fake_urlopen(routes)):
            self.assertTrue(wd.is_master_healthy())
        self.assertEqual(wd.master_instance_id, "master_new")
        self.assertTrue(wd.is_direct_verified)


if __name__ == "__main__":
    unittest.main()
