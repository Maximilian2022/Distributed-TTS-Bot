import unittest
from src.utils.latency_tracker import LatencyTracker, PlaybackLatencyRecord


def make_record(**kwargs) -> PlaybackLatencyRecord:
    defaults = {
        "timestamp": "12:00:00",
        "user_name": "TestUser",
        "text_preview": "テスト本文",
        "char_count": 10,
        "speaker_name": "テスト話者",
        "provider": "google",
        "worker_label": "TestNode",
        "discord_latency_ms": 100.0,
        "queue_wait_ms": 0.0,
        "synthesis_ms": 500.0,
        "play_prep_ms": 5.0,
        "total_latency_ms": 605.0,
        "system_latency_ms": 505.0,
        "is_cached": False,
        "is_fallback": False,
        "status_level": "fair",
    }
    defaults.update(kwargs)
    return PlaybackLatencyRecord(**defaults)


class TestLatencyTrackerClassification(unittest.TestCase):
    def test_text_split_overhead(self):
        """split_ms が 1000ms 以上の場合は TEXT_SPLIT_OVERHEAD 原因タグが付与されること"""
        record = make_record(
            turn_ttfs_ms=1800.0,
            split_ms=1600.0,
            synthesis_ms=200.0,
            chunks=[{
                "chunk_index": 0,
                "provider": "google",
                "synth_ms": 200.0,
                "duration_ms": 1500.0,
                "is_cached": False,
            }]
        )
        ctx = {"expected_provider": "google"}
        types, causes = LatencyTracker._analyze_incident(record, ctx)
        self.assertIn("C_SLOW_FIRST_AUDIO", types)
        self.assertIn("TEXT_SPLIT_OVERHEAD", causes)
        self.assertNotIn("UNCLASSIFIED", causes)

    def test_cloud_api_latency_chunk(self):
        """クラウドTTS APIで synth_ms が 1200ms 以上の場合は CLOUD_API_LATENCY_CHUNK が付与されること"""
        record = make_record(
            turn_ttfs_ms=1600.0,
            synthesis_ms=1550.0,
            chunks=[{
                "chunk_index": 1,
                "provider": "edge",
                "synth_ms": 1550.0,
                "duration_ms": 2000.0,
                "is_cached": False,
            }]
        )
        ctx = {"expected_provider": "edge"}
        types, causes = LatencyTracker._analyze_incident(record, ctx)
        self.assertIn("C_SLOW_FIRST_AUDIO", types)
        self.assertIn("CLOUD_API_LATENCY_CHUNK_1", causes)
        self.assertNotIn("UNCLASSIFIED", causes)

    def test_worker_slow_chunk(self):
        """ローカルエンジンで synth_ms が 1500ms 以上の場合は WORKER_SLOW_CHUNK が付与されること"""
        record = make_record(
            turn_ttfs_ms=1700.0,
            synthesis_ms=1650.0,
            chunks=[{
                "chunk_index": 1,
                "provider": "voicevox",
                "synth_ms": 1650.0,
                "duration_ms": 1800.0,
                "is_cached": False,
            }]
        )
        ctx = {"expected_provider": "voicevox"}
        types, causes = LatencyTracker._analyze_incident(record, ctx)
        self.assertIn("C_SLOW_FIRST_AUDIO", types)
        self.assertIn("WORKER_SLOW_CHUNK_1", causes)
        self.assertNotIn("UNCLASSIFIED", causes)

    def test_short_text_rtf_spike(self):
        """極短文 (1〜3文字) で API 往復により RTF > 1.2 となった場合は SHORT_TEXT_RTF_SPIKE が付与されること"""
        record = make_record(
            turn_ttfs_ms=900.0,
            rtf=2.5,
            char_count=1,
            synthesis_ms=850.0,
            chunks=[{
                "chunk_index": 0,
                "provider": "google",
                "synth_ms": 850.0,
                "duration_ms": 340.0,
                "is_cached": False,
            }]
        )
        ctx = {"expected_provider": "google"}
        types, causes = LatencyTracker._analyze_incident(record, ctx)
        self.assertIn("D_RTF_OVER", types)
        self.assertIn("SHORT_TEXT_RTF_SPIKE", causes)
        self.assertNotIn("UNCLASSIFIED", causes)

    def test_voice_pipeline_stall(self):
        """推論や分割以外のシステム未説明待機が 2000ms 以上の場合は VOICE_PIPELINE_STALL が付与されること"""
        record = make_record(
            turn_ttfs_ms=5000.0,
            system_latency_ms=5000.0,
            synthesis_ms=300.0,
            split_ms=50.0,
            preroll_wait_ms=100.0,
            queue_wait_ms=10.0,
            play_prep_ms=5.0,
            chunks=[]
        )
        ctx = {"expected_provider": "azure"}
        types, causes = LatencyTracker._analyze_incident(record, ctx)
        self.assertIn("C_SLOW_FIRST_AUDIO", types)
        self.assertIn("VOICE_PIPELINE_STALL", causes)
        self.assertNotIn("UNCLASSIFIED", causes)


if __name__ == "__main__":
    unittest.main()
