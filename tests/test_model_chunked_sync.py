"""WAN 越しのモデル同期: Master の分割アップロード API と、Agent の分割アップロード / Range 分割取得"""
import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import aiohttp
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from src.config import AppConfig
from src.web.server import WebServer

TOKEN = "shared_secret_chunk"


def _load_agent():
    with patch.dict(os.environ, {"WORKER_SHARED_SECRET": TOKEN}), patch.object(sys, "argv", ["agent.py"]):
        path = os.path.join(os.path.dirname(__file__), "..", "src", "web", "static", "agent.py")
        spec = importlib.util.spec_from_file_location("agent_chunk_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _model_tar(model: str, payload_bytes: int) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        def add(name, data):
            ti = tarfile.TarInfo(f"{model}/{name}")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        add("metas.json", json.dumps({"speakerUuid": "11111111-2222-3333-4444-555555555555"}).encode())
        add("model.bin", os.urandom(payload_bytes))
    return buf.getvalue()


class _Base(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.model_dir = Path(self.tmp.name) / "models"
        self.model_dir.mkdir()
        self.env = patch.dict(os.environ, {"COEIROINK_MODEL_DIR": str(self.model_dir)})
        self.env.start()
        config = AppConfig()
        config.worker_shared_secret = TOKEN
        config.db_path = os.path.join(self.tmp.name, "bot.db")
        self.web_server = WebServer(bot=MagicMock(), config=config, database=MagicMock(),
                                    cache_manager=MagicMock(), tts_manager=MagicMock())
        return self.web_server.app

    async def tearDownAsync(self):
        self.env.stop()
        self.tmp.cleanup()

    H = {"X-Worker-Token": TOKEN}


class TestChunkedUploadApi(_Base):
    async def _init(self, data: bytes, chunk: int, model="m_test"):
        r = await self.client.post(f"/api/worker/models/chunked/{model}/init", headers=self.H,
                                   json={"total_size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "chunk_size": chunk})
        return r.status, await r.json()

    @unittest_run_loop
    async def test_full_flow_with_resume(self):
        data = _model_tar("m_test", 300_000)
        chunk = 100_000
        status, init = await self._init(data, chunk)
        self.assertEqual(status, 200)
        uid = init["upload_id"]
        parts = [data[i:i + chunk] for i in range(0, len(data), chunk)]
        # 1 つ目だけ送ってから再 init → 受信済みが返る (再開)
        r = await self.client.put(f"/api/worker/models/chunked/{uid}/chunk/0", data=parts[0],
                                  headers=dict(self.H, **{"X-Chunk-SHA256": hashlib.sha256(parts[0]).hexdigest()}))
        self.assertEqual(r.status, 200)
        _, again = await self._init(data, chunk)
        self.assertEqual(again["upload_id"], uid)
        self.assertEqual(again["received"], [0])
        # 欠けたまま complete → 409
        self.assertEqual((await self.client.post(f"/api/worker/models/chunked/{uid}/complete", headers=self.H)).status, 409)
        for i, p in enumerate(parts[1:], start=1):
            r = await self.client.put(f"/api/worker/models/chunked/{uid}/chunk/{i}", data=p, headers=self.H)
            self.assertEqual(r.status, 200)
        r = await self.client.post(f"/api/worker/models/chunked/{uid}/complete", headers=self.H)
        self.assertEqual(r.status, 200, await r.text())
        self.assertTrue((self.model_dir / "m_test" / "metas.json").exists())
        # 配置済みモデルの再 init は already_exists
        _, ex = await self._init(data, chunk)
        self.assertEqual(ex["status"], "already_exists")

    @unittest_run_loop
    async def test_rejections(self):
        data = _model_tar("m_bad", 50_000)
        _, init = await self._init(data, 40_000, model="m_bad")
        uid = init["upload_id"]
        # チャンクのハッシュ不一致
        r = await self.client.put(f"/api/worker/models/chunked/{uid}/chunk/0", data=data[:40_000],
                                  headers=dict(self.H, **{"X-Chunk-SHA256": "0" * 64}))
        self.assertEqual(r.status, 422)
        # サイズ不一致
        r = await self.client.put(f"/api/worker/models/chunked/{uid}/chunk/0", data=data[:1000], headers=self.H)
        self.assertEqual(r.status, 400)
        # 範囲外 index / 不明 upload_id
        self.assertEqual((await self.client.put(f"/api/worker/models/chunked/{uid}/chunk/99", data=b"x", headers=self.H)).status, 400)
        self.assertEqual((await self.client.get(f"/api/worker/models/chunked/{'0' * 32}/status", headers=self.H)).status, 404)
        # 上限超え (チャンク 50MB 超)
        r = await self.client.post("/api/worker/models/chunked/m_big/init", headers=self.H,
                                   json={"total_size": 10, "sha256": "a" * 64, "chunk_size": 60 * 1024 * 1024})
        self.assertEqual(r.status, 413)
        # WAN からのトークンなしはミドルウェアで拒否
        r = await self.client.post("/api/worker/models/chunked/m_x/init", headers={"CF-Connecting-IP": "203.0.113.5"},
                                   json={"total_size": 10, "sha256": "a" * 64})
        self.assertEqual(r.status, 403)

    @unittest_run_loop
    async def test_archive_checksum_mismatch(self):
        data = _model_tar("m_sum", 20_000)
        r = await self.client.post("/api/worker/models/chunked/m_sum/init", headers=self.H,
                                   json={"total_size": len(data), "sha256": "b" * 64, "chunk_size": len(data)})
        uid = (await r.json())["upload_id"]
        await self.client.put(f"/api/worker/models/chunked/{uid}/chunk/0", data=data, headers=self.H)
        r = await self.client.post(f"/api/worker/models/chunked/{uid}/complete", headers=self.H)
        self.assertEqual(r.status, 422)
        self.assertFalse((self.model_dir / "m_sum").exists())


class _FakeAgent:
    """WorkerAgent のメソッドだけを、実体の重い初期化なしで呼ぶための最小オブジェクト"""

    def __init__(self, session, chunk_bytes):
        self._s = session
        self.MODEL_CHUNK_BYTES = chunk_bytes
        self.any_human_in_vc = False

    async def get_session(self):
        return self._s


class TestAgentChunkedTransfer(_Base):
    @unittest_run_loop
    async def test_agent_chunked_upload_installs_model(self):
        agent = _load_agent()
        tar_bytes = _model_tar("m_agent", 250_000)
        tar_path = Path(self.tmp.name) / "m_agent.tar"
        tar_path.write_bytes(tar_bytes)
        async with aiohttp.ClientSession() as s:
            fake = _FakeAgent(s, 64 * 1024)
            ok = await agent.WorkerAgent._chunked_upload_model(fake, str(self.server.make_url("")).rstrip("/"), "m_agent", tar_path, dict(self.H))
        self.assertTrue(ok)
        self.assertTrue((self.model_dir / "m_agent" / "metas.json").exists())

    @unittest_run_loop
    async def test_agent_range_download_in_small_parts(self):
        agent = _load_agent()
        d = self.model_dir / "m_range"
        d.mkdir()
        (d / "metas.json").write_text("{}")
        blob = os.urandom(700_000)
        (d / "big.bin").write_bytes(blob)
        url = str(self.server.make_url("/api/worker/models/file/m_range/big.bin"))
        out = Path(self.tmp.name) / "out.bin"
        async with aiohttp.ClientSession() as s:
            fake = _FakeAgent(s, 100_000)  # 100KB 単位 → 7 分割
            ok = await agent.WorkerAgent._parallel_range_download(fake, url, str(out), len(blob), dict(self.H), connections=4)
        self.assertTrue(ok)
        self.assertEqual(out.read_bytes(), blob)


if __name__ == "__main__":
    unittest.main()
