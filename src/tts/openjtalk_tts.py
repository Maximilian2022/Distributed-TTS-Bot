import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from src.tts.base import BaseTTSClient, TTSError

logger = logging.getLogger(__name__)

class OpenJTalkTTSClient(BaseTTSClient):
    """
    Open JTalk を用いた超低遅延・完全ローカル音声合成クライアント
    - 出力形式: 24kHz, 16bit, Mono PCM WAV (標準出力をインメモリ直接受取)
    - 名工大メイ (5種)、名工大たくみ (4種)、名工大男声 (nitech)、ゆっくり風 (SofTalk調)
    - CPU負荷・メモリ消費ほぼゼロ、通信遅延ゼロ
    """

    VOICES = {
        "ojt_mei_normal": {
            "name": "メイ (ノーマル)",
            "file": "mei_normal.htsvoice",
            "gender": "female",
            "description": "OpenJTalk: 名工大 メイ (ノーマル)",
            "base_fm": 0.0,
            "jf": 1.0,
        },
        "ojt_mei_happy": {
            "name": "メイ (ハッピー)",
            "file": "mei_happy.htsvoice",
            "gender": "female",
            "description": "OpenJTalk: 名工大 メイ (ハッピー)",
            "base_fm": 0.0,
            "jf": 1.0,
        },
        "ojt_mei_angry": {
            "name": "メイ (怒り)",
            "file": "mei_angry.htsvoice",
            "gender": "female",
            "description": "OpenJTalk: 名工大 メイ (怒り)",
            "base_fm": 0.0,
            "jf": 1.0,
        },
        "ojt_mei_sad": {
            "name": "メイ (悲しみ)",
            "file": "mei_sad.htsvoice",
            "gender": "female",
            "description": "OpenJTalk: 名工大 メイ (悲しみ)",
            "base_fm": 0.0,
            "jf": 1.0,
        },
        "ojt_mei_bashful": {
            "name": "メイ (ツンツン)",
            "file": "mei_bashful.htsvoice",
            "gender": "female",
            "description": "OpenJTalk: 名工大 メイ (ツンツン)",
            "base_fm": 0.0,
            "jf": 1.0,
        },
        "ojt_takumi_normal": {
            "name": "たくみ (ノーマル)",
            "file": "takumi_normal.htsvoice",
            "gender": "male",
            "description": "OpenJTalk: 名工大 たくみ (ノーマル)",
            "base_fm": 0.0,
            "jf": 1.0,
        },
        "ojt_takumi_happy": {
            "name": "たくみ (ハッピー)",
            "file": "takumi_happy.htsvoice",
            "gender": "male",
            "description": "OpenJTalk: 名工大 たくみ (ハッピー)",
            "base_fm": 0.0,
            "jf": 1.0,
        },
        "ojt_takumi_angry": {
            "name": "たくみ (怒り)",
            "file": "takumi_angry.htsvoice",
            "gender": "male",
            "description": "OpenJTalk: 名工大 たくみ (怒り)",
            "base_fm": 0.0,
            "jf": 1.0,
        },
        "ojt_takumi_sad": {
            "name": "たくみ (悲しみ)",
            "file": "takumi_sad.htsvoice",
            "gender": "male",
            "description": "OpenJTalk: 名工大 たくみ (悲しみ)",
            "base_fm": 0.0,
            "jf": 1.0,
        },
        "ojt_nitech": {
            "name": "名工大 男声",
            "file": "nitech.htsvoice",
            "gender": "male",
            "description": "OpenJTalk: 名古屋工業大学 標準男声",
            "base_fm": 0.0,
            "jf": 1.0,
        },
        "ojt_yukkuri": {
            "name": "ゆっくり風 (SofTalk調)",
            "file": "mei_normal.htsvoice",
            "gender": "female",
            "description": "OpenJTalk: ゆっくり風 (機械調・抑揚低め)",
            "base_fm": 3.0,
            "jf": 0.2,
        },
    }

    def __init__(
        self,
        voices_dir: str = "/app/openjtalk_voices",
        dic_dir: str = "/var/lib/mecab/dic/open-jtalk/naist-jdic",
        timeout_seconds: float = 5.0
    ):
        self.voices_dir = Path(voices_dir)
        self.dic_dir = Path(dic_dir)
        self.timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "openjtalk"

    def is_available(self) -> bool:
        # 辞書ディレクトリとボイスディレクトリが存在するか
        return self.dic_dir.exists() and self.voices_dir.exists()

    async def close(self) -> None:
        pass

    @staticmethod
    def _convert_pitch_to_halftone(pitch_str: str, base_fm: float = 0.0) -> float:
        """
        Azure等のパーセント表記 (+10%, -5%) を OpenJTalk の追加半音 (-fm: half-tone) に変換
        10% = 約1半音 (+1.0)
        """
        try:
            cleaned = str(pitch_str).replace("%", "").replace("+", "").strip()
            val = float(cleaned)
            # 10% あたり 1.0 半音シフト
            return base_fm + (val / 10.0)
        except (ValueError, TypeError):
            return base_fm

    async def synthesize(
        self,
        text: str,
        voice_name: str,
        pitch: str = "0%",
        speed: float = 1.0
    ) -> bytes:
        """
        Open JTalk で音声を合成し、WAVバイナリを返す
        """
        self.last_worker_label = "OpenJTalk (Local CPU)"
        # voice_nameの解決 (ojt_mei_normal 等、または voice_nameに直接ファイル名が入っている場合)
        voice_key = voice_name
        if voice_key not in self.VOICES:
            # プレフィックスなしでも検索
            prefixed = f"ojt_{voice_key}"
            if prefixed in self.VOICES:
                voice_key = prefixed
            else:
                voice_key = "ojt_mei_normal"

        v_info = self.VOICES.get(voice_key, self.VOICES["ojt_mei_normal"])
        htsvoice_path = self.voices_dir / v_info["file"]
        if not htsvoice_path.exists():
            # フォールバック: ディレクトリ内の最初の .htsvoice
            fallback_voices = list(self.voices_dir.glob("*.htsvoice"))
            if fallback_voices:
                htsvoice_path = fallback_voices[0]
            else:
                raise TTSError(f"No htsvoice files found in {self.voices_dir}")

        fm = self._convert_pitch_to_halftone(pitch, base_fm=v_info.get("base_fm", 0.0))
        jf = v_info.get("jf", 1.0)
        speaking_rate = max(0.3, min(3.0, float(speed)))

        cmd = [
            "open_jtalk",
            "-x", str(self.dic_dir),
            "-m", str(htsvoice_path),
            "-s", "24000",
            "-r", str(speaking_rate),
            "-fm", str(fm),
            "-jf", str(jf),
            "-ow", "/dev/stdout",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=text.encode("utf-8")),
                timeout=self.timeout_seconds
            )

            if proc.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="replace").strip()
                logger.error(f"OpenJTalk synthesis error (code {proc.returncode}): {err_msg}")
                raise TTSError(f"OpenJTalk returned exit code {proc.returncode}: {err_msg}")

            if not stdout:
                raise TTSError("OpenJTalk produced empty audio data.")

            return stdout

        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise TTSError(f"OpenJTalk synthesis timed out after {self.timeout_seconds}s")
        except Exception as e:
            if isinstance(e, TTSError):
                raise
            logger.error(f"Unexpected error in OpenJTalk synthesis: {e}")
            raise TTSError(f"OpenJTalk synthesis failed: {e}") from e
