"""_stream_play_item を偽エンジン・偽VCで最後まで動かし、段階導入フラグ (off/shadow/on) ごとの配線を検証する"""
import asyncio
import io
import time
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import patch

from src.config import VoiceConfig
from src.cogs.vc_handler import VCHandlerCog, QueueItem


def _wav(sec: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * int(24000 * sec))
    return buf.getvalue()


DEBIAN = {"label": "Debian", "pending_ms": 0.0, "est_ms": (lambda L: 78 + 55.4 * L)}
UBUNTU = {"label": "Ubuntu", "pending_ms": 0.0, "est_ms": (lambda L: 54 + 63.8 * L)}
TEXT = ("うほうほうっほうほうほうっほほほほわらドンッドンッドォンッ(大地を叩く音)"
        "うほっうほうほうっほわらうほっっほーっほーっわらうーほっうほうほうほうほうほうほわら")


class FakeTTS:
    def __init__(self):
        self.calls = []
        self.cache_manager = SimpleNamespace(get=self._cache_get)
        self.voice = VoiceConfig(id="ci", name="金苗", gender="female", description="", provider="coeiroink", voice_name="uuid_0")

    async def _cache_get(self, key, provider="azure"):
        return None

    def _get_voice_config(self, speaker_id):
        return self.voice

    def _get_worker_label_for_provider(self, prov):
        return prov

    def get_expected_worker_info(self, provider, model_name=None, text_len=10, guild_id=None):
        return {"worker_label": "MasterDebian (CPU)", "has_gpu": False, "is_model_loaded": True, "estimated_node_ms": None}

    def peek_earliest_finish(self, provider, text_len, model_name=None, guild_id=None):
        return {"worker_label": "MasterDebian (CPU)", "has_gpu": False, "eta_ms": 78 + 55.4 * text_len}

    def get_planning_nodes(self, provider, model_name=None, guild_id=None):
        return [DEBIAN, UBUNTU]

    def get_last_chunk_meta(self, text, speaker_id):
        return {"joined_inflight": None}

    async def get_chunk_audio(self, text, speaker_id, pitch, speed, user_id=0, is_longest_chunk=False,
                              speculative_timeout=None, speculative_deadline_fn=None, prefer_earliest_finish=False,
                              local_only=False, avoid_nodes=None, **kwargs):
        self.calls.append({
            "text": text,
            "deadline_rel": (speculative_deadline_fn() - time.time()) if speculative_deadline_fn else None,
            "prefer": prefer_earliest_finish,
        })
        if text.endswith("、"):
            return _wav(0.4), "coeiroink", "L1/L2 キャッシュ", True
        await asyncio.sleep(0.01 * len(text) / 10)
        return _wav(0.02 * len(text)), "coeiroink", "MasterDebian (CPU) / COEIROINK", False


class FakeVC:
    def __init__(self, loop):
        self.loop = loop

    def play(self, source, after=None):
        async def drain():
            # ストリームが finish されるまで読み出し、再生完了を通知
            while True:
                frame = source.read()
                if not frame:
                    break
                await asyncio.sleep(0)
            after(None)
        self.loop.create_task(drain())


def _make_cog(flags: dict):
    cog = VCHandlerCog.__new__(VCHandlerCog)
    loop = asyncio.get_running_loop()
    cog.bot = SimpleNamespace(loop=loop)
    cog.config = SimpleNamespace(
        voices={}, speculative_timeout_seconds=4.0, max_silence_default_seconds=1.5,
        az_margin_seconds=0.45, head_plan_pessimism=1.5, **flags,
    )

    async def get_guild_settings(gid, cfg=None):
        return {"speculative_timeout_seconds": 4.0, "max_silence_seconds": 1.5}
    cog.db = SimpleNamespace(get_guild_settings=get_guild_settings)
    cog.tts_manager = FakeTTS()
    return cog


def _flags(mode: str) -> dict:
    return {k: mode for k in ("stream_tl_speculative", "head_route_earliest", "head_dynamic_length", "chunk_singleflight")}


class TestStreamPlayItemFlags(unittest.TestCase):
    def _run(self, mode: str, author_prefix="かつさん、"):
        recorded = {}

        def fake_record(**kwargs):
            recorded.update(kwargs)

        async def run():
            cog = _make_cog(_flags(mode))
            state = SimpleNamespace(play_finished_event=asyncio.Event(), deduplicator=SimpleNamespace(record_played=lambda t: None))
            item = QueueItem(text=TEXT, user_id=1, user_name="u", speaker_id="ci", pitch="0", speed=1.0,
                             author_prefix=author_prefix, speculative_timeout=4.0)
            guild = SimpleNamespace(id=1, name="g")
            with patch("src.cogs.vc_handler.latency_tracker.record", side_effect=fake_record), \
                 patch("src.utils.text_cleaner.latency_profiler.get_dynamic_max_chars", return_value=30):
                ok = await asyncio.wait_for(cog._stream_play_item(guild, state, FakeVC(asyncio.get_running_loop()), item, time.time()), 10)
            return ok, cog.tts_manager.calls
        ok, calls = asyncio.run(run())
        return ok, calls, recorded

    def test_off_keeps_legacy_wiring(self):
        ok, calls, rec = self._run("off")
        self.assertTrue(ok)
        self.assertTrue(all(c["deadline_rel"] is None and not c["prefer"] for c in calls))
        self.assertEqual(len(calls[1]["text"]), 30)
        self.assertNotIn("tl_deadline_rel_ms", rec["chunks"][1])

    def test_shadow_logs_without_changing_behavior(self):
        ok, calls, rec = self._run("shadow")
        self.assertTrue(ok)
        self.assertTrue(all(c["deadline_rel"] is None and not c["prefer"] for c in calls))
        self.assertEqual(len(calls[1]["text"]), 30)  # 先頭は短縮されない
        head = rec["chunks"][1]
        self.assertIn("tl_deadline_rel_ms", head)
        self.assertIn("legacy_spec_ms", head)
        self.assertIsNotNone(head.get("head_chars_planned"))  # CPU のみ構成なので短縮計画は出る
        self.assertEqual(head.get("pred_node"), "MasterDebian (CPU)")

    def test_on_applies_deadline_route_and_head(self):
        ok, calls, rec = self._run("on")
        self.assertTrue(ok)
        body = [c for c in calls if not c["text"].endswith("、")]
        self.assertTrue(all(c["deadline_rel"] is not None for c in body))
        # 先頭本文のみノード指定
        self.assertTrue(body[0]["prefer"])
        self.assertFalse(any(c["prefer"] for c in body[1:]))
        # 先頭本文は短縮され、名前の再生後 (最大許容沈黙 + 名前長 - マージン) の締切を持つ
        self.assertLess(len(body[0]["text"]), 30)
        self.assertGreater(body[0]["deadline_rel"], 1.0)
        # 後続ほど締切が遠い (一律 4.0s の打ち切りが無い)
        self.assertGreater(body[-1]["deadline_rel"], body[1]["deadline_rel"])
        self.assertEqual(rec["chunks"][1].get("head_chars_applied"), len(body[0]["text"]))
        self.assertEqual("".join(c["text"] for c in body), TEXT)

    def test_on_without_name_routes_head_first(self):
        ok, calls, rec = self._run("on", author_prefix=None)
        self.assertTrue(ok)
        self.assertTrue(calls[0]["prefer"])  # 先頭本文が最初に発注される
        self.assertLess(len(calls[0]["text"]), 30)
        self.assertLess(calls[0]["deadline_rel"], 1.5)  # 最初の音まで 1.5s - マージン


if __name__ == "__main__":
    unittest.main()
