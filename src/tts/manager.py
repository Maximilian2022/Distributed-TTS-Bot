import os
import time
import io
import json
import wave
import asyncio
import logging
import aiohttp
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

from src.cache_manager import CacheManager
from src.config import AppConfig, VoiceConfig
from src.utils.text_cleaner import TextCleaner, TextChunk
from src.utils.async_task import safe_create_task
from .base import BaseTTSClient, RateLimitError, TTSConnectionError, TTSError
from .azure_tts import AzureTTSClient
from .google_tts import GoogleTTSClient
from .voicevox_tts import VoicevoxTTSClient
from .sharevox_tts import SharevoxTTSClient
from .coeiroink_tts import CoeiroinkTTSClient, build_coeiroink_warmup_payload
from .edge_tts import EdgeTTSClient
from .openjtalk_tts import OpenJTalkTTSClient
from .node_pool import TTSNodePool
from src.resource_manager import HighLoadFallbackException, SynthesisTimeoutException

logger = logging.getLogger(__name__)

class TTSManager:
    """
    TTS統合マネージャー
    - スマートパーツ分割・並列合成・ゼロ遅延WAV連結 (無音ギャップ自動挿入)
    - マルチノード / 分散コンピュートワーカー管理 (TTSNodePool)
    - ユーザーID分離＆量子化ピッチ・速度による全体共有キャッシュ (LRU mtime touch)
    - Azure AI Speech / Google Cloud TTS / Edge-TTS / VOICEVOX / SHAREVOX / COEIROINK / OpenJTalk 7系統統合
    - ローカルエンジン (VOICEVOX, SHAREVOX, COEIROINK) のCPU競合を防ぐ直列化排他制御 (asyncio.Lock)
    - Google Cloud TTS 無料枠95% (950,000文字) 到達時の自動課金防止迂回ガード
    - 多層フォールバック: Edge/Azure ➔ Google ➔ ローカル系 (完全ローカル・セーフティネット)
    - クラウド用 (10GB) と ローカル専用 (20GB) の完全分離キャッシュ管理
    """

    VOICES_CACHE_FILE = "/app/data/voices_cache.json"

    def __init__(self, config: AppConfig, cache_manager: CacheManager, database=None, resource_manager=None):
        self.config = config
        self.cache_manager = cache_manager
        self.db = database
        self.resource_manager = resource_manager
        if self.db and hasattr(self.cache_manager, "set_database"):
            self.cache_manager.set_database(self.db)

        # 各エンジンのマルチノードプール初期化 (共用マシン配慮のリソース上限付き)
        self.voicevox_pool = TTSNodePool("voicevox", getattr(config, "voicevox_nodes", []))
        self.sharevox_pool = TTSNodePool("sharevox", getattr(config, "sharevox_nodes", []))
        self.coeiroink_pool = TTSNodePool("coeiroink", getattr(config, "coeiroink_nodes", []))
        self.ws_worker_nodes: Dict[str, Any] = {}

        # 各TTSクライアントの初期化
        self.azure_client = AzureTTSClient(
            key=config.azure_speech_key,
            region=config.azure_speech_region,
            timeout_seconds=config.azure_timeout_seconds
        )
        self.google_client = GoogleTTSClient(
            credentials_path=config.google_application_credentials,
            api_key=config.google_api_key,
            credentials_json=config.google_credentials_json,
            endpoint=config.google_endpoint,
            timeout_seconds=config.google_timeout_seconds
        )
        self.voicevox_client = VoicevoxTTSClient(
            base_url=config.voicevox_base_url,
            timeout_seconds=config.voicevox_timeout_seconds,
            node_pool=self.voicevox_pool
        )
        self.sharevox_client = SharevoxTTSClient(
            base_url=config.sharevox_base_url,
            timeout_seconds=config.sharevox_timeout_seconds,
            node_pool=self.sharevox_pool
        )
        self.coeiroink_client = CoeiroinkTTSClient(
            base_url=config.coeiroink_base_url,
            timeout_seconds=config.coeiroink_timeout_seconds,
            node_pool=self.coeiroink_pool
        )
        self.edge_client = EdgeTTSClient(
            timeout_seconds=8.0
        )
        self.openjtalk_client = OpenJTalkTTSClient(
            voices_dir=config.openjtalk_voices_dir,
            dic_dir=config.openjtalk_dic_dir,
            timeout_seconds=config.openjtalk_timeout_seconds
        )

        # ローカルニューラルエンジン専用の直列化排他ロック (CPU競合・遅延急増の完全防止)
        self._local_synthesis_lock = asyncio.Lock()

        # 直近の合成メタ情報 (レイテンシ計測・WebUI表示用)
        self.last_synthesis_meta: Dict[str, Any] = {}

        # Google Cloud TTS 月間文字数キャッシュ (TTL: 60秒) (PERF-4)
        self._google_monthly_chars_cache: Tuple[float, int] = (0.0, 0)

        # 永続キャッシュからローカルエンジン話者を即座に復元 (停止時・再起動時でも一覧を保持)
        self.load_cached_voices()

    def reload_cloud_credentials(
        self,
        azure_key: Optional[str] = None,
        azure_region: Optional[str] = None,
        google_json: Optional[str] = None,
        google_api_key: Optional[str] = None
    ) -> None:
        """WebUI等からクラウドAPIキーが更新された際にクライアントを動的再初期化"""
        if azure_key is not None:
            self.config.azure_speech_key = azure_key.strip()
        if azure_region is not None:
            self.config.azure_speech_region = azure_region.strip()
        if google_json is not None:
            self.config.google_credentials_json = google_json.strip()
        if google_api_key is not None:
            self.config.google_api_key = google_api_key.strip()

        # Azure クライアントの再初期化
        old_azure = self.azure_client
        self.azure_client = AzureTTSClient(
            key=self.config.azure_speech_key,
            region=self.config.azure_speech_region or "japaneast",
            timeout_seconds=self.config.azure_timeout_seconds
        )
        if old_azure:
            safe_create_task(old_azure.close(), name="close_old_azure_client")

        # Google クライアントの再初期化
        old_google = self.google_client
        self.google_client = GoogleTTSClient(
            credentials_path=self.config.google_application_credentials,
            api_key=self.config.google_api_key,
            credentials_json=self.config.google_credentials_json,
            endpoint=self.config.google_endpoint,
            timeout_seconds=self.config.google_timeout_seconds
        )
        if old_google:
            safe_create_task(old_google.close(), name="close_old_google_client")

        logger.info(
            f"🔄 [TTSManager] Cloud credentials reloaded: "
            f"Azure={'Available' if self.azure_client.is_available() else 'Disabled'}, "
            f"Google={'Available' if self.google_client.is_available() else 'Disabled'}"
        )

    async def test_synthesis(self, provider: str, voice_name: str = "") -> Tuple[bool, str, float]:
        """指定プロバイダの疎通・音声合成テストを実施 (成否, メッセージ, 所要ミリ秒)"""
        start_ts = time.time()
        test_text = "こんにちは、テスト音声です。"
        prov = provider.lower().strip()

        try:
            if prov == "azure":
                if not self.azure_client.is_available():
                    return False, "Azure Speech API key is not configured.", 0.0
                v = voice_name or "ja-JP-NanamiNeural"
                wav = await self.azure_client.synthesize(test_text, v)
            elif prov == "google":
                if not self.google_client.is_available():
                    return False, "Google Cloud credentials are not configured.", 0.0
                v = voice_name or "ja-JP-Neural2-B"
                wav = await self.google_client.synthesize(test_text, v)
            elif prov == "edge":
                v = voice_name or "ja-JP-NanamiNeural"
                wav = await self.edge_client.synthesize(test_text, v)
            elif prov == "openjtalk":
                v = voice_name or "ojt_mei_normal"
                wav = await self.openjtalk_client.synthesize(test_text, v)
            else:
                return False, f"Unsupported provider for testing: {prov}", 0.0

            elapsed_ms = round((time.time() - start_ts) * 1000, 1)
            if wav and len(wav) > 44:
                return True, f"Success ({len(wav)} bytes generated)", elapsed_ms
            else:
                return False, "Empty or invalid WAV returned", elapsed_ms
        except Exception as e:
            elapsed_ms = round((time.time() - start_ts) * 1000, 1)
            logger.warning(f"Test synthesis failed for {prov}: {e}")
            return False, str(e), elapsed_ms

    def load_cached_voices(self) -> int:
        """保存された voices_cache.json からローカルエンジンモデルを config.voices に復元"""
        if not os.path.exists(self.VOICES_CACHE_FILE):
            return 0
        try:
            with open(self.VOICES_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            loaded_cnt = 0
            for item in data.get("voices", []):
                v_id = item.get("id")
                if v_id and v_id not in self.config.voices:
                    self.config.voices[v_id] = VoiceConfig(
                        id=v_id,
                        name=item.get("name", v_id),
                        gender=item.get("gender", "neutral"),
                        description=item.get("description", ""),
                        provider=item.get("provider", "edge"),
                        voice_name=item.get("voice_name", ""),
                        style_id=item.get("style_id"),
                        fallback_provider=item.get("fallback_provider", "edge"),
                        fallback_voice=item.get("fallback_voice", "ja-JP-NanamiNeural")
                    )
                    loaded_cnt += 1
            if loaded_cnt > 0:
                logger.info(f"Loaded {loaded_cnt} local engine voice models from disk cache ({self.VOICES_CACHE_FILE}).")
            return loaded_cnt
        except Exception as e:
            logger.warning(f"Failed to load voices cache: {e}")
            return 0

    def save_voices_cache(self) -> None:
        """ローカルエンジン (VOICEVOX / SHAREVOX / COEIROINK) の話者定義を永続保存"""
        local_providers = {"voicevox", "sharevox", "coeiroink"}
        records = []
        for v in self.config.voices.values():
            if v.provider in local_providers:
                records.append({
                    "id": v.id,
                    "name": v.name,
                    "gender": v.gender,
                    "description": v.description,
                    "provider": v.provider,
                    "voice_name": v.voice_name,
                    "style_id": v.style_id,
                    "fallback_provider": v.fallback_provider,
                    "fallback_voice": v.fallback_voice
                })
        try:
            os.makedirs(os.path.dirname(self.VOICES_CACHE_FILE), exist_ok=True)
            with open(self.VOICES_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"voices": records, "updated_at": int(time.time())}, f, ensure_ascii=False, indent=2)
            logger.info(f"Saved {len(records)} local voice models to persistent cache at {self.VOICES_CACHE_FILE}")
        except Exception as e:
            logger.error(f"Failed to save voices cache: {e}")

    def _is_local_provider(self, provider: str) -> bool:
        """プロバイダーがローカル推論エンジン系（VOICEVOX等）かどうか判定"""
        p = str(provider).lower()
        return p in self.cache_manager.LOCAL_PROVIDERS or p.startswith("vv_")

    async def initialize_voicevox(self) -> int:
        """
        VOICEVOX Engine から全キャラクター・全スタイル情報を動的取得し、レジストリに登録
        """
        try:
            speakers = await self.voicevox_client.get_speakers()
            if not speakers:
                logger.info("VOICEVOX Engine is idle or stopped; loaded from cache.")
                return 0

            count = 0
            for spk in speakers:
                char_name = spk.get("name", "")
                styles = spk.get("styles", [])
                for style in styles:
                    style_id = style.get("id")
                    style_name = style.get("name", "")
                    voice_id = f"vv_{style_id}"

                    # 性別の推定
                    female_keywords = ["めたん", "ずんだ", "つむぎ", "はう", "リツ", "ひまり", "そら", "WhiteCUL", "ミコ", "小夜", "ナナ", "アル", "ビィ", "うさぎ", "ニア", "花丸", "もち子", "後鬼", "No.7", "ナースロボ", "あいえるたん", "ぞん子", "つるぎ"]
                    gender = "female" if any(k in char_name for k in female_keywords) else "male"

                    self.config.voices[voice_id] = VoiceConfig(
                        id=voice_id,
                        name=f"{char_name} ({style_name})",
                        gender=gender,
                        description=f"VOICEVOX: {char_name} - {style_name}",
                        provider="voicevox",
                        voice_name=str(style_id),
                        style_id=style_id,
                        fallback_provider="azure",
                        fallback_voice="ja-JP-NanamiNeural"
                    )
                    count += 1

            if count > 0:
                self.save_voices_cache()
            logger.info(f"Registered {count} VOICEVOX voice models dynamically into registry.")
            return count
        except Exception as e:
            logger.error(f"Error during VOICEVOX dynamic voice initialization: {e}")
            return 0

    async def initialize_sharevox(self) -> int:
        """
        SHAREVOX Engine から全キャラクター・全スタイル情報を動的取得し、レジストリに登録
        """
        try:
            speakers = await self.sharevox_client.get_speakers()
            if not speakers:
                logger.info("SHAREVOX Engine is idle or stopped; loaded from cache.")
                return 0

            count = 0
            male_keywords = ["Yくん", "開発者"]
            for spk in speakers:
                char_name = spk.get("name", "")
                styles = spk.get("styles", [])
                gender = "male" if any(k in char_name for k in male_keywords) else "female"

                for style in styles:
                    style_id = style.get("id")
                    style_name = style.get("name", "")
                    voice_id = f"sv_{style_id}"

                    self.config.voices[voice_id] = VoiceConfig(
                        id=voice_id,
                        name=f"{char_name} ({style_name})",
                        gender=gender,
                        description=f"SHAREVOX: {char_name} - {style_name}",
                        provider="sharevox",
                        voice_name=str(style_id),
                        style_id=style_id,
                        fallback_provider="edge",
                        fallback_voice="ja-JP-NanamiNeural"
                    )
                    count += 1

            if count > 0:
                self.save_voices_cache()
            logger.info(f"Registered {count} SHAREVOX voice models dynamically into registry.")
            return count
        except Exception as e:
            logger.error(f"Error during SHAREVOX dynamic voice initialization: {e}")
            return 0

    async def initialize_coeiroink(self) -> int:
        """
        COEIROINK Engine (API) およびディスク上のローカルモデルディレクトリ (/app/coeiroink_models) から
        実在するキャラクター・スタイル情報のみを厳密に取得し、レジストリに登録。
        未実在スタイル（実機に物理モデルが存在しないもの）はキャッシュからも完全パージする。
        """
        try:
            speakers_map: Dict[str, Dict[str, Any]] = {}
            api_reachable = False

            # 1. 稼働中の COEIROINK Engine API からスピーカー一覧を取得 (これが唯一の真実)
            try:
                speakers = await self.coeiroink_client.get_speakers()
                if speakers:
                    api_reachable = True
                    for spk in speakers:
                        u = str(spk.get("speakerUuid", "")).strip().lower()
                        if u:
                            speakers_map[u] = spk
            except Exception as e:
                logger.debug(f"COEIROINK engine API not reachable (container may be idle): {e}")

            # 2. Engine API が取得できなかった場合のみ、ディスク上のモデルフォルダから物理モデル実在スタイルのみ補完
            if not api_reachable:
                model_dir = Path(os.getenv("COEIROINK_MODEL_DIR", "/app/coeiroink_models"))
                if model_dir.exists():
                    for entry in model_dir.iterdir():
                        if entry.is_dir():
                            meta_file = entry / "metas.json"
                            if meta_file.exists():
                                try:
                                    with open(meta_file, "r", encoding="utf-8") as f:
                                        meta = json.load(f)
                                        u = str(meta.get("speakerUuid", "")).strip().lower()
                                        model_sub_dir = entry / "model"
                                        raw_styles = meta.get("styles", [])
                                        valid_styles = []
                                        # model/{styleId} ディレクトリが実際に存在するもののみ (C-2: 事前set走査でO(1)化)
                                        if model_sub_dir.exists():
                                            existing_style_dirs = {p.name for p in model_sub_dir.iterdir() if p.is_dir()}
                                            for st in raw_styles:
                                                sid = str(st.get("styleId", ""))
                                                if sid and sid in existing_style_dirs:
                                                    valid_styles.append(st)
                                        meta["styles"] = valid_styles
                                        if u and valid_styles:
                                            speakers_map[u] = meta
                                except Exception as e:
                                    logger.debug(f"Error reading metas.json from {entry}: {e}")

            if not speakers_map:
                logger.warning("No speakers found from COEIROINK Engine or local disk.")
                return 0

            # 実在スタイルをレジストリに登録
            registered_voice_ids = set()
            count = 0
            for speaker_uuid, spk in speakers_map.items():
                char_name = spk.get("speakerName", "")
                styles = spk.get("styles", [])

                for style in styles:
                    style_id = style.get("styleId")
                    style_name = style.get("styleName", "")
                    voice_id = f"ci_{style_id}"
                    # 同一style_id重複回避のため、既に存在する場合はuuidを含める
                    if voice_id in self.config.voices and getattr(self.config.voices[voice_id], "voice_name", "") != f"{speaker_uuid}_{style_id}":
                        voice_id = f"ci_{speaker_uuid[:8]}_{style_id}"

                    # 性別の推定
                    male_keywords = ["男", "少年", "青年", "おじ", "男性"]
                    gender = "male" if any(k in char_name for k in male_keywords) else "female"

                    self.config.voices[voice_id] = VoiceConfig(
                        id=voice_id,
                        name=f"{char_name} ({style_name})",
                        gender=gender,
                        description=f"COEIROINK: {char_name} - {style_name}",
                        provider="coeiroink",
                        voice_name=f"{speaker_uuid}_{style_id}",
                        style_id=style_id,
                        fallback_provider="edge",
                        fallback_voice="ja-JP-NanamiNeural"
                    )
                    registered_voice_ids.add(voice_id)
                    count += 1

            # 3. 過去のキャッシュ等から読み込まれた「実機に存在しない未実在スタイル」を完全パージ
            purged_ids = []
            for vid, v in list(self.config.voices.items()):
                if v.provider == "coeiroink":
                    if vid not in registered_voice_ids:
                        self.config.voices.pop(vid, None)
                        purged_ids.append(vid)

            if purged_ids:
                logger.warning(f"Purged {len(purged_ids)} non-existent COEIROINK styles from voice registry: {purged_ids[:10]}...")

            if count > 0 or purged_ids:
                self.save_voices_cache()

            logger.info(f"Registered {count} COEIROINK voice models dynamically into registry (purged {len(purged_ids)} invalid styles).")
            return count
        except Exception as e:
            logger.error(f"Error during COEIROINK dynamic voice initialization: {e}", exc_info=True)
            return 0

    async def close(self) -> None:
        """リソースの解放"""
        await self.azure_client.close()
        await self.google_client.close()
        await self.voicevox_client.close()
        await self.sharevox_client.close()
        await self.coeiroink_client.close()
        await self.edge_client.close()
        await self.openjtalk_client.close()

    def register_ws_worker_node(self, ws_node: Any):
        """ボランティア型 WebSocket ワーカーノードを対応プールに動的配備"""
        self.ws_worker_nodes[ws_node.id] = ws_node
        for eng in ws_node.supported_engines:
            if eng == "voicevox":
                self.voicevox_pool.add_node(ws_node)
            elif eng == "coeiroink":
                self.coeiroink_pool.add_node(ws_node)
            elif eng == "sharevox":
                self.sharevox_pool.add_node(ws_node)
        logger.info(f"✨ [TTSManager] WS Worker '{ws_node.id}' registered for engines: {ws_node.supported_engines}")

    def unregister_ws_worker_node(self, node_id: str):
        """切断された WebSocket ワーカーノードを全プールから安全に離脱"""
        ws_node = self.ws_worker_nodes.pop(node_id, None)
        if ws_node:
            for eng in ws_node.supported_engines:
                if eng == "voicevox":
                    self.voicevox_pool.remove_node(node_id)
                elif eng == "coeiroink":
                    self.coeiroink_pool.remove_node(node_id)
                elif eng == "sharevox":
                    self.sharevox_pool.remove_node(node_id)
            logger.info(f"🔌 [TTSManager] WS Worker '{node_id}' unregistered from pools.")

    async def start_heartbeat_monitor(self, interval_sec: float = 30.0):
        """全TTSノードプールおよびボランティアノードの定期ハートビート監視ループ"""
        logger.info("🩺 [TTSManager] Node heartbeat monitor loop started.")
        while True:
            try:
                await asyncio.sleep(interval_sec)
                # 1. 各プールの定期タイムアウトチェック
                self.voicevox_pool.check_heartbeat_timeout(timeout_seconds=60.0)
                self.sharevox_pool.check_heartbeat_timeout(timeout_seconds=60.0)
                self.coeiroink_pool.check_heartbeat_timeout(timeout_seconds=60.0)

                # 2. WebSocket ボランティアノードのゾンビ接続・切断チェック
                now = time.time()
                dead_nodes = []
                for n_id, ws_node in list(self.ws_worker_nodes.items()):
                    if getattr(ws_node.ws, "closed", True) or (now - ws_node.last_heartbeat > 60.0):
                        dead_nodes.append(n_id)

                for dead_id in dead_nodes:
                    logger.warning(f"⚠️ [TTSManager] WS Worker '{dead_id}' heartbeat timed out or WS closed. Auto-unregistering.")
                    self.unregister_ws_worker_node(dead_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in TTSManager heartbeat monitor: {e}", exc_info=True)

    def _get_voice_config(self, speaker_id: str) -> VoiceConfig:
        """話者IDからボイス定義を取得"""
        if speaker_id in self.config.voices:
            return self.config.voices[speaker_id]
        if self.config.default_speaker in self.config.voices:
            return self.config.voices[self.config.default_speaker]
        # 設定が空の場合のフォールバック
        return VoiceConfig(
            id=speaker_id,
            name="Nanami",
            gender="female",
            description="デフォルト話者",
            azure_voice="ja-JP-NanamiNeural",
            google_voice="ja-JP-Neural2-B",
            provider="azure"
        )

    def _get_worker_label_for_provider(self, provider: str) -> str:
        """プロバイダーごとの直近処理ワーカー名を取得"""
        client_map = {
            "voicevox": self.voicevox_client,
            "sharevox": self.sharevox_client,
            "coeiroink": self.coeiroink_client,
            "edge": self.edge_client,
            "azure": self.azure_client,
            "google": self.google_client,
            "openjtalk": self.openjtalk_client,
        }
        client = client_map.get(provider)
        if client:
            return getattr(client, "last_worker_label", provider)
        return provider

    @staticmethod
    def concatenate_wavs(wav_bytes_list: List[bytes], pause_durations_ms: Optional[List[int]] = None) -> bytes:
        """
        複数の 24kHz/16bit/Mono PCM WAV バイナリをインメモリで超低遅延（0.05ms）連結。
        パーツ間に自然な無音PCMギャップをミリ秒単位で直接挿入する。
        """
        if not wav_bytes_list:
            return b""
        if len(wav_bytes_list) == 1:
            return wav_bytes_list[0]

        pcm_frames = []
        params = None

        for idx, wav_bytes in enumerate(wav_bytes_list):
            if not wav_bytes:
                continue
            try:
                with wave.open(io.BytesIO(wav_bytes), 'rb') as r:
                    if params is None:
                        params = r.getparams()
                    frames = r.readframes(r.getnframes())
                    pcm_frames.append(frames)

                    # パーツ間の無音ギャップ挿入 (末尾パーツの後には挿入しない)
                    if pause_durations_ms and idx < len(pause_durations_ms) and idx < len(wav_bytes_list) - 1:
                        ms = pause_durations_ms[idx]
                        if ms > 0 and params:
                            bytes_per_ms = int(params.framerate * params.sampwidth * params.nchannels / 1000)
                            pcm_frames.append(b'\x00' * (bytes_per_ms * ms))
            except Exception as e:
                logger.error(f"Error reading WAV frames during concatenation: {e}")
                continue

        if not pcm_frames or params is None:
            return wav_bytes_list[0] if wav_bytes_list else b""

        out_io = io.BytesIO()
        with wave.open(out_io, 'wb') as w:
            # 連結後のフレーム数を 0 から安全に開始 (writeframes/close時に実フレーム数でヘッダーが確定される)
            # 入力WAVの巨大フレーム数 (0x7FFFFFFF等) によるヘッダーオーバーフロー ('L' format error) を完全防止
            w.setparams((
                params.nchannels,
                params.sampwidth,
                params.framerate,
                0,
                params.comptype,
                params.compname
            ))
            w.writeframes(b"".join(pcm_frames))
        return out_io.getvalue()

    async def _try_local_engine(
        self,
        engine_name: str,
        client: Any,
        pool: Any,
        text: str,
        style_val: str,
        pitch: str,
        speed: float,
        is_longest_chunk: bool = False
    ) -> Optional[bytes]:
        """
        ローカルTTSエンジン（VOICEVOX, SHAREVOX, COEIROINK）の共通合成処理 (A-5, E-4)。
        外部GPUワーカーの先行実行、コンテナ自動起動、スロット排他ロック、障害ハンドリングを一元化。
        """
        if not client.is_available():
            return None

        # 外部GPUノードが稼働している場合は、Master自機コンテナ起動とホスト排他ロックをバイパスして即時並行実行
        if pool.has_active_external_gpu_node():
            try:
                return await client.synthesize(
                    text=text,
                    voice_name=style_val,
                    pitch=pitch,
                    speed=speed,
                    is_longest_chunk=is_longest_chunk
                )
            except Exception as e:
                logger.warning(f"{engine_name.upper()} external GPU chunk synthesis failed: {e}")

        # 外部GPUノードがおらず、外部CPUノードのみまたはMaster自機が必要な場合:
        if self.resource_manager:
            ready = await self.resource_manager.start_engine(engine_name, wait_healthy=True, timeout=8.0)
            if not ready:
                return None
        slot_ctx = self.resource_manager.acquire_synthesis_slot(engine_name) if self.resource_manager else self._local_synthesis_lock
        try:
            async with slot_ctx:
                res = await client.synthesize(
                    text=text,
                    voice_name=style_val,
                    pitch=pitch,
                    speed=speed,
                    is_longest_chunk=is_longest_chunk
                )
                if res and self.resource_manager:
                    self.resource_manager.mark_engine_used(engine_name)
                return res
        except (HighLoadFallbackException, SynthesisTimeoutException) as e:
            logger.info(f"{engine_name.upper()} deferred/bypassed by adaptive load controller: {e}")
            return None
        except Exception as e:
            logger.warning(f"{engine_name.upper()} chunk synthesis failed: {e}")
        return None

    async def _synthesize_single_chunk(
        self,
        text: str,
        speaker_id: str,
        pitch: str,
        speed: float,
        is_longest_chunk: bool = False,
        speculative_timeout: Optional[float] = None
    ) -> Tuple[bytes, str, str]:
        """
        単一のテキストパーツを合成。
        - プライマリプロバイダーを最優先
        - Google 95% (950,000文字) 到達時は課金防止のため自動バイパス
        - 障害時は Azure ➔ Google ➔ VOICEVOX の多層セーフティネットで自動フォールバック
        """
        voice_cfg = self._get_voice_config(speaker_id)
        primary_provider = voice_cfg.provider

        async def _try_voicevox(style_val: str) -> Optional[bytes]:
            return await self._try_local_engine("voicevox", self.voicevox_client, self.voicevox_pool, text, style_val, pitch, speed, is_longest_chunk)

        async def _try_sharevox(style_val: str) -> Optional[bytes]:
            return await self._try_local_engine("sharevox", self.sharevox_client, self.sharevox_pool, text, style_val, pitch, speed, is_longest_chunk)

        async def _try_coeiroink(voice_val: str) -> Optional[bytes]:
            return await self._try_local_engine("coeiroink", self.coeiroink_client, self.coeiroink_pool, text, voice_val, pitch, speed, is_longest_chunk)

        preview_text = text[:30] + ("..." if len(text) > 30 else "")

        async def _try_edge(voice_name: str) -> Optional[bytes]:
            if not self.edge_client.is_available():
                return None
            try:
                t0 = time.time()
                res = await self.edge_client.synthesize(
                    text=text,
                    voice_name=voice_name,
                    pitch=pitch,
                    speed=speed
                )
                if res:
                    logger.info(f"☁️ [EDGE-TTS] 音声合成完了 ({(time.time()-t0)*1000:.0f}ms): クラウドAPI | text='{preview_text}'")
                return res
            except Exception as e:
                logger.warning(f"Edge-TTS chunk synthesis failed: {e}")
            return None

        async def _try_azure(voice_name: str) -> Optional[bytes]:
            if not self.azure_client.is_available():
                return None
            try:
                t0 = time.time()
                res = await self.azure_client.synthesize(
                    text=text,
                    voice_name=voice_name,
                    pitch=pitch,
                    speed=speed
                )
                if res:
                    logger.info(f"☁️ [AZURE] 音声合成完了 ({(time.time()-t0)*1000:.0f}ms): クラウドAPI | text='{preview_text}'")
                return res
            except (RateLimitError, TTSConnectionError) as e:
                logger.warning(f"Azure Speech chunk synthesis failed: {e}")
            except Exception as e:
                logger.error(f"Unexpected error in Azure Speech chunk synthesis: {e}")
            return None

        async def _try_google(voice_name: str) -> Optional[bytes]:
            if not self.google_client.is_available():
                return None
            # Google Cloud TTS 無料枠95% (950,000文字) 上限ガード (PERF-4: インメモリキャッシュ化 TTL 60s)
            if self.db:
                now_ts = time.time()
                last_t, cached_used = self._google_monthly_chars_cache
                if now_ts - last_t > 60.0:
                    try:
                        used = await self.db.get_provider_monthly_characters("google")
                        self._google_monthly_chars_cache = (now_ts, used)
                    except Exception as e:
                        logger.warning(f"Error checking Google TTS usage limit: {e}")
                        used = cached_used
                else:
                    used = cached_used

                limit = getattr(self.config, "google_monthly_limit_chars", 950000)
                if used >= limit:
                    logger.warning(
                        f"Google Cloud TTS 95% limit reached ({used}/{limit} chars). "
                        f"Bypassing Google to protect from billing."
                    )
                    return None

            try:
                t0 = time.time()
                res = await self.google_client.synthesize(
                    text=text,
                    voice_name=voice_name,
                    pitch=pitch,
                    speed=speed
                )
                if res:
                    logger.info(f"☁️ [GOOGLE] 音声合成完了 ({(time.time()-t0)*1000:.0f}ms): クラウドAPI | text='{preview_text}'")
                    # キャッシュ文字数をインクリメント
                    t_c, u_c = self._google_monthly_chars_cache
                    self._google_monthly_chars_cache = (t_c, u_c + len(text))
                return res
            except Exception as e:
                logger.warning(f"Google Cloud TTS chunk synthesis failed: {e}")
            return None

        async def _try_openjtalk(voice_name: str) -> Optional[bytes]:
            if not self.openjtalk_client.is_available():
                return None
            try:
                t0 = time.time()
                res = await self.openjtalk_client.synthesize(
                    text=text,
                    voice_name=voice_name,
                    pitch=pitch,
                    speed=speed
                )
                if res:
                    logger.info(f"📢 [OPENJTALK] 音声合成完了 ({(time.time()-t0)*1000:.0f}ms): ローカルエンジン | text='{preview_text}'")
                return res
            except Exception as e:
                logger.warning(f"OpenJTalk chunk synthesis failed: {e}")
            return None

        default_vv_style = str(getattr(self.config, "voicevox_default_fallback_style_id", 3))

        def _notify_fallback(orig_prov: str, fb_prov: str):
            try:
                from src.utils.alert_service import alert_service, CATEGORY_MASTER_ENGINE, SEVERITY_WARNING
                alert_service.notify(
                    title=f"TTSエンジン障害・{fb_prov.upper()}退避",
                    message=f"エンジン `{orig_prov.upper()}` が応答不能となったため、`{fb_prov.upper()}` へフォールバック退避しました。\n対象テキスト: `{text[:30]}`",
                    category=CATEGORY_MASTER_ENGINE,
                    severity=SEVERITY_WARNING,
                    engine_name=orig_prov.upper()
                )
            except Exception:
                pass

        def _is_valid_wav(data: Optional[bytes]) -> bool:
            if not data or len(data) <= 44:
                return False
            if not data.startswith(b"RIFF"):
                return False
            return True

        # プロバイダーごとの合成呼び出しディスパッチャ (C-1)
        async def _call_provider(prov: str, voice_val: str) -> Optional[bytes]:
            res: Optional[bytes] = None
            if prov == "voicevox":
                res = await _try_voicevox(voice_val)
            elif prov == "sharevox":
                res = await _try_sharevox(voice_val)
            elif prov == "coeiroink":
                res = await _try_coeiroink(voice_val)
            elif prov == "edge":
                res = await _try_edge(voice_val)
            elif prov == "azure":
                res = await _try_azure(voice_val)
            elif prov == "google":
                res = await _try_google(voice_val)
            elif prov == "openjtalk":
                res = await _try_openjtalk(voice_val)

            if res is not None and not _is_valid_wav(res):
                logger.warning(f"⚠️ Provider '{prov}' returned empty or invalid WAV ({len(res)} bytes). Treating as failure.")
                return None
            return res

        edge_fallback_voice = "ja-JP-KeitaNeural" if getattr(voice_cfg, "gender", "female") == "male" else "ja-JP-NanamiNeural"

        # プライマリプロバイダーに応じたフォールバック計画テーブル (C-1)
        fallback_chains: Dict[str, List[Tuple[str, str]]] = {
            "voicevox": [
                ("voicevox", voice_cfg.get_primary_voice()),
                ("edge", edge_fallback_voice),
                ("azure", voice_cfg.get_fallback_voice()),
                ("google", "ja-JP-Neural2-B"),
                ("openjtalk", "ojt_mei_normal"),
            ],
            "sharevox": [
                ("sharevox", voice_cfg.get_primary_voice()),
                ("edge", edge_fallback_voice),
                ("azure", voice_cfg.get_fallback_voice()),
                ("google", "ja-JP-Neural2-B"),
                ("openjtalk", "ojt_mei_normal"),
            ],
            "coeiroink": [
                ("coeiroink", voice_cfg.get_primary_voice()),
                ("edge", edge_fallback_voice),
                ("azure", voice_cfg.get_fallback_voice()),
                ("google", "ja-JP-Neural2-B"),
                ("openjtalk", "ojt_mei_normal"),
            ],
            "openjtalk": [
                ("openjtalk", voice_cfg.get_primary_voice()),
                ("edge", edge_fallback_voice),
                ("azure", voice_cfg.get_fallback_voice()),
                ("google", "ja-JP-Neural2-B"),
            ],
            "edge": [
                ("edge", voice_cfg.get_primary_voice()),
                ("azure", voice_cfg.get_fallback_voice()),
                ("google", "ja-JP-Neural2-B"),
                ("voicevox", default_vv_style),
                ("openjtalk", "ojt_mei_normal"),
            ],
            "google": [
                ("google", voice_cfg.get_primary_voice()),
                ("edge", edge_fallback_voice),
                ("azure", voice_cfg.get_fallback_voice()),
                ("voicevox", default_vv_style),
                ("openjtalk", "ojt_mei_normal"),
            ],
            "azure": [
                ("azure", voice_cfg.get_primary_voice()),
                ("edge", edge_fallback_voice),
                ("google", voice_cfg.get_fallback_voice()),
                ("voicevox", default_vv_style),
                ("openjtalk", "ojt_mei_normal"),
            ],
        }

        chain = fallback_chains.get(primary_provider) or fallback_chains["azure"]

        # 投機的デュアルストリーミング (Speculative Dual-Streaming) (IDEA-2):
        # プライマリがローカルGPUエンジンの場合、1500ms経過しても応答がなければ即座にクラウド (Edge/Azure) に並行投機リクエストを発行
        if primary_provider in ("voicevox", "sharevox", "coeiroink") and len(chain) > 1:
            prim_prov, prim_param = chain[0]
            cloud_prov, cloud_param = chain[1]

            if speculative_timeout is None:
                speculative_timeout = float(os.getenv("SPECULATIVE_TIMEOUT_SECONDS", getattr(self.config, "speculative_timeout_seconds", 1.5)))
            prim_task = asyncio.create_task(_call_provider(prim_prov, prim_param))
            try:
                # 1500ms 待機 (ローカルGPU推論の完了を十分に粘り、キャラ声維持率を最大化)
                done, _ = await asyncio.wait([prim_task], timeout=speculative_timeout)
                if prim_task in done:
                    audio = prim_task.result()
                    if audio is not None and _is_valid_wav(audio):
                        return audio, prim_prov, prim_param
            except Exception:
                pass

            # 1500ms超過かつプライマリ未完了: クラウド投機並行リクエストを発動！
            if not prim_task.done():
                logger.info(
                    f"⚡ [Speculative TTS] {prim_prov.upper()} synthesis took >{speculative_timeout*1000:.0f}ms for '{preview_text}'. "
                    f"Launching speculative {cloud_prov.upper()} request concurrently."
                )
                cloud_task = asyncio.create_task(_call_provider(cloud_prov, cloud_param))

                # 先に成功した方を即時採用！
                pending = {prim_task, cloud_task}
                while pending:
                    done_set, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for finished_task in done_set:
                        res_audio = None
                        try:
                            res_audio = finished_task.result()
                        except Exception:
                            res_audio = None

                        if res_audio is not None and _is_valid_wav(res_audio):
                            winner_prov, winner_param = (prim_prov, prim_param) if finished_task is prim_task else (cloud_prov, cloud_param)
                            if finished_task is cloud_task:
                                logger.info(f"🚀 [Speculative TTS] Cloud fallback ({cloud_prov.upper()}) responded first! Adopting to eliminate conversation stall.")
                                if not prim_task.done():
                                    prim_task.cancel()
                            else:
                                if not cloud_task.done():
                                    cloud_task.cancel()
                            return res_audio, winner_prov, winner_param

                # 両方並行実行して両方失敗した場合は、上位2プロバイダを消費して次へ
                chain = chain[2:]
            else:
                # プライマリが即座に失敗（None/例外/無効WAV）した場合:
                # プライマリのみを消費し、cloud_prov（Edge-TTS）を含む以降のチェーンへ進む（スキップ防止）
                chain = chain[1:]

        for step_idx, (prov, voice_param) in enumerate(chain):
            if step_idx > 0:
                prev_prov = chain[step_idx - 1][0]
                logger.info(f"{prev_prov.upper()} failed/bypassed for '{text[:15]}...', falling back to {prov.upper()}.")
                if step_idx == 1 and primary_provider in ("voicevox", "sharevox", "coeiroink"):
                    _notify_fallback(primary_provider, prov)

            audio = await _call_provider(prov, voice_param)
            if audio is not None:
                return audio, prov, voice_param

        err_summary = f"All TTS providers (VOICEVOX, Edge-TTS, Azure, Google, OpenJTalk) failed for chunk '{text[:15]}...'"
        try:
            from src.utils.alert_service import alert_service, CATEGORY_MASTER_ENGINE, SEVERITY_CRITICAL
            alert_service.notify(
                title="全TTSエンジン完全停止・合成不能",
                message=f"すべてのTTSエンジン（ローカルVOICEVOX/SHAREVOX/COEIROINK、Edge-TTS、Azure、Google、OpenJTalk）での音声生成に失敗しました。\n対象テキスト: `{text[:50]}`",
                category=CATEGORY_MASTER_ENGINE,
                severity=SEVERITY_CRITICAL,
                engine_name=primary_provider.upper()
            )
        except Exception:
            pass
        raise TTSError(err_summary)
    async def get_chunk_audio(
        self,
        text: str,
        speaker_id: str,
        pitch: str,
        speed: float,
        user_id: int = 0,
        is_longest_chunk: bool = False,
        speculative_timeout: Optional[float] = None
    ) -> Tuple[bytes, str, str, bool]:
        """
        単一チャンク（文パーツや発言者名プレフィックス等）の音声バイト列を取得。
        1. L1/L2キャッシュ確認 (ヒット時は0ms即答)
        2. ミス時は最優先GPU/クラウドワーカーで合成
        3. キャッシュ保存 & DB記録
        戻り値: (audio_bytes, provider, worker_label, is_cached)
        """
        voice_cfg = self._get_voice_config(speaker_id)
        provider = voice_cfg.provider
        cache_key = CacheManager.generate_cache_key(speaker_id, pitch, speed, text, user_id=user_id)

        # 1. キャッシュ確認
        cached_bytes = await self.cache_manager.get_bytes(cache_key, provider=provider)
        if cached_bytes is not None:
            if self.db:
                safe_create_task(
                    self.db.record_cache_hit(len(text), is_local=self._is_local_provider(provider)),
                    name="record_cache_hit_chunk"
                )
            return cached_bytes, provider, "L1/L2 キャッシュ", True

        # 2. 合成実行
        audio_bytes, used_provider, used_param = await self._synthesize_single_chunk(
            text, speaker_id, pitch, speed, is_longest_chunk=is_longest_chunk, speculative_timeout=speculative_timeout
        )
        worker_lbl = self._get_worker_label_for_provider(used_provider)

        # 3. キャッシュ保存 & 利用量記録
        # Speculative TTSまたはフォールバックでクラウドが採用された場合 (used_provider != provider):
        # 元のモデル (provider) のキャッシュキー・ローカルキャッシュには絶対に保存しない。
        # 実際に合成されたクラウド話者 (used_param) のキーでクラウドキャッシュ (used_provider) として保存する。
        if used_provider != provider:
            fb_cache_key = CacheManager.generate_cache_key(used_param, pitch, speed, text, user_id=user_id)
            safe_create_task(
                self.cache_manager.save(
                    fb_cache_key,
                    audio_bytes,
                    provider=used_provider,
                    text=text,
                    speaker_id=used_param,
                    speaker_name=f"{used_provider.upper()} ({used_param})",
                    is_chunk=True
                ),
                name="save_chunk_cache_fallback"
            )
        else:
            speaker_name = voice_cfg.name if voice_cfg else speaker_id
            safe_create_task(
                self.cache_manager.save(
                    cache_key,
                    audio_bytes,
                    provider=provider,
                    text=text,
                    speaker_id=speaker_id,
                    speaker_name=speaker_name,
                    is_chunk=True
                ),
                name="save_chunk_cache"
            )
        if self.db and used_provider:
            safe_create_task(
                self.db.record_tts_usage(used_provider, len(text)),
                name="record_tts_usage_chunk"
            )

        return audio_bytes, used_provider, worker_lbl, False

    async def get_audio_path(
        self,
        user_id: int,
        speaker_id: str,
        pitch: str,
        speed: float,
        text: str,
        author_prefix: Optional[str] = None,
        chunks: Optional[List[TextChunk]] = None,
        speculative_timeout: Optional[float] = None
    ) -> Path:
        """
        スマートパーツ分割・並列合成・ゼロ遅延WAV結合による音声ファイル取得。
        1. 全文キャッシュの超高速確認 (ヒット時は分割・結合すらスキップ)
        2. チャンク分割 (発言者名プレフィックス + 句読点文境界)
        3. 各チャンクのキャッシュ確認 (ヒット時は節約文字数を記録)
        4. ミスしたチャンクの並列TTS合成 (asyncio.gather) & パーツキャッシュ保存
        5. インメモリWAV連結 (自然な無音ギャップ自動挿入)
        6. 全文キャッシュ保存 & 返却
        """
        voice_cfg = self._get_voice_config(speaker_id)
        provider = voice_cfg.provider

        full_text = f"{author_prefix}{text}" if author_prefix else text
        full_cache_key = CacheManager.generate_cache_key(speaker_id, pitch, speed, full_text, user_id=user_id)

        # 1. 全文キャッシュ確認 (超高速パス)
        cached_file = await self.cache_manager.get(full_cache_key, provider=provider)
        if cached_file is not None:
            logger.debug(f"Full cache hit ({provider}): {full_cache_key}")
            self.last_synthesis_meta = {
                "worker": "L1/L2 キャッシュ",
                "is_cached": True,
                "is_fallback": False,
                "provider": provider
            }
            if self.db:
                safe_create_task(self.db.record_cache_hit(len(full_text), is_local=self._is_local_provider(provider)), name="record_cache_hit_full")
            return cached_file

        # 2. チャンクの取得 (未指定なら split_into_chunks で自動分割)
        if chunks is None:
            chunks = TextCleaner.split_into_chunks(text, author_prefix=author_prefix)

        voice_cfg = self.config.voices.get(speaker_id)
        speaker_name = voice_cfg.name if voice_cfg else speaker_id

        # 単一チャンクかつプレフィックス無しの場合はシンプル処理
        if len(chunks) == 1 and not author_prefix and chunks[0].text == text:
            single_chunk = chunks[0]
            chunk_cache_key = full_cache_key
            cached_bytes = await self.cache_manager.get_bytes(chunk_cache_key, provider=provider)
            if cached_bytes is not None:
                self.last_synthesis_meta = {
                    "worker": "L1/L2 キャッシュ",
                    "is_cached": True,
                    "is_fallback": False,
                    "provider": provider
                }
                if self.db:
                    safe_create_task(self.db.record_cache_hit(len(single_chunk.text), is_local=self._is_local_provider(provider)), name="record_cache_hit_single")
                return self.cache_manager.get_file_path(chunk_cache_key, provider=provider)

            audio_bytes, used_provider, used_param = await self._synthesize_single_chunk(
                single_chunk.text, speaker_id, pitch, speed, speculative_timeout=speculative_timeout
            )
            worker_lbl = self._get_worker_label_for_provider(used_provider)
            self.last_synthesis_meta = {
                "worker": worker_lbl,
                "is_cached": False,
                "is_fallback": (used_provider != provider),
                "provider": used_provider
            }
            if self.db and used_provider:
                safe_create_task(self.db.record_tts_usage(used_provider, len(single_chunk.text)), name="record_tts_usage_single")

            # 投機またはフォールバックでクラウドが採用された場合は、クラウド話者のキーでクラウドキャッシュへ保存
            if used_provider != provider:
                fb_key = CacheManager.generate_cache_key(used_param, pitch, speed, single_chunk.text, user_id=user_id)
                return await self.cache_manager.save(
                    fb_key,
                    audio_bytes,
                    provider=used_provider,
                    text=single_chunk.text,
                    speaker_id=used_param,
                    speaker_name=f"{used_provider.upper()} ({used_param})",
                    is_chunk=False
                )
            else:
                return await self.cache_manager.save(
                    chunk_cache_key,
                    audio_bytes,
                    provider=provider,
                    text=single_chunk.text,
                    speaker_id=speaker_id,
                    speaker_name=speaker_name,
                    is_chunk=False
                )

        # 3. 複数チャンクのキャッシュ確認 (並列 I/O 走査)
        chunk_results: List[Optional[bytes]] = [None] * len(chunks)
        missed_indices: List[int] = []

        async def _check_chunk(i: int, ch: TextChunk):
            k = CacheManager.generate_cache_key(speaker_id, pitch, speed, ch.text, user_id=user_id)
            b = await self.cache_manager.get_bytes(k, provider=provider)
            return i, ch, b

        check_results = await asyncio.gather(*[_check_chunk(i, c) for i, c in enumerate(chunks)])
        for idx, chunk, c_bytes in check_results:
            if c_bytes is not None:
                chunk_results[idx] = c_bytes
                logger.debug(f"Chunk cache hit ({provider}): '{chunk.text[:10]}...' ({len(chunk.text)} chars)")
                if self.db:
                    safe_create_task(self.db.record_cache_hit(len(chunk.text), is_local=self._is_local_provider(provider)), name="record_cache_hit_chunk")
            else:
                missed_indices.append(idx)

        # 4. キャッシュミスしたチャンクの並列合成 (asyncio.gather)
        last_used_provider = provider
        if missed_indices:
            logger.info(
                f"Synthesizing {len(missed_indices)} missed chunk(s) in parallel (total chunks: {len(chunks)})..."
            )
            longest_idx = max(missed_indices, key=lambda idx: len(chunks[idx].text))
            synth_tasks = [
                self._synthesize_single_chunk(
                    chunks[i].text,
                    speaker_id,
                    pitch,
                    speed,
                    is_longest_chunk=(i == longest_idx and len(chunks[i].text) >= 15),
                    speculative_timeout=speculative_timeout
                )
                for i in missed_indices
            ]
            synth_results = await asyncio.gather(*synth_tasks)

            has_fallback_chunk = False
            for i, (audio_bytes, used_provider, used_param) in zip(missed_indices, synth_results):
                chunk_results[i] = audio_bytes
                last_used_provider = used_provider
                # 個別チャンクをキャッシュに保存 (将来のヒット用、メタデータ付き)
                # 投機またはフォールバックでクラウドが採用された場合は、クラウド話者のキーでクラウドキャッシュへ保存
                if used_provider != provider:
                    has_fallback_chunk = True
                    fb_key = CacheManager.generate_cache_key(used_param, pitch, speed, chunks[i].text, user_id=user_id)
                    safe_create_task(self.cache_manager.save(
                        fb_key,
                        audio_bytes,
                        provider=used_provider,
                        text=chunks[i].text,
                        speaker_id=used_param,
                        speaker_name=f"{used_provider.upper()} ({used_param})",
                        is_chunk=True
                    ), name=f"save_chunk_cache_{i}")
                else:
                    c_key = CacheManager.generate_cache_key(speaker_id, pitch, speed, chunks[i].text, user_id=user_id)
                    safe_create_task(self.cache_manager.save(
                        c_key,
                        audio_bytes,
                        provider=provider,
                        text=chunks[i].text,
                        speaker_id=speaker_id,
                        speaker_name=speaker_name,
                        is_chunk=True
                    ), name=f"save_chunk_cache_{i}")
                if self.db and used_provider:
                    safe_create_task(self.db.record_tts_usage(used_provider, len(chunks[i].text)), name=f"record_tts_usage_chunk_{i}")
        worker_lbl = self._get_worker_label_for_provider(last_used_provider) if missed_indices else "L1/L2 キャッシュ"
        self.last_synthesis_meta = {
            "worker": worker_lbl,
            "is_cached": (len(missed_indices) == 0),
            "is_fallback": (last_used_provider != provider or has_fallback_chunk),
            "provider": last_used_provider
        }

        # 5. インメモリWAV連結 (無音ギャップ自動挿入)
        valid_wavs = [w for w in chunk_results if w is not None]
        pause_durations = [chunk.pause_after_ms for chunk in chunks]
        final_wav_bytes = self.concatenate_wavs(valid_wavs, pause_durations)

        # 6. 全文キャッシュとして保存して返却 (メタデータ付き)
        # フォールバック音声が含まれていた場合、本来のモデルキーに汚染が混入するのを防ぐ
        if has_fallback_chunk or last_used_provider != provider:
            logger.debug(f"Bypassing full-text cache for '{speaker_id}' due to fallback provider usage ({last_used_provider})")
            fallback_voice = getattr(voice_cfg, "fallback_voice", "ja-JP-NanamiNeural")
            fb_full_key = CacheManager.generate_cache_key(fallback_voice, pitch, speed, full_text, user_id=user_id)
            saved_path = await self.cache_manager.save(
                fb_full_key,
                final_wav_bytes,
                provider=last_used_provider,
                text=full_text,
                speaker_id=fallback_voice,
                speaker_name=f"{last_used_provider.upper()} Fallback",
                is_chunk=False
            )
        else:
            saved_path = await self.cache_manager.save(
                full_cache_key,
                final_wav_bytes,
                provider=provider,
                text=full_text,
                speaker_id=speaker_id,
                speaker_name=speaker_name,
                is_chunk=False
            )
        return saved_path

    async def warmup_speaker(self, speaker_id: str) -> None:
        """
        指定話者のモデルを、登録されているすべての利用可能ノード（GPU/CPUワーカー）に事前初期化リクエストを並行送信。
        重複送信防止キャッシュおよびGPUノード存在時のCPUノードダミー推論スキップを適用。
        """
        voice_cfg = self._get_voice_config(speaker_id)
        if not voice_cfg:
            return
        provider = voice_cfg.provider
        if not self._is_local_provider(provider):
            return

        pool_map = {
            "voicevox": self.voicevox_pool,
            "sharevox": self.sharevox_pool,
            "coeiroink": self.coeiroink_pool,
        }
        pool = pool_map.get(provider)
        if not pool:
            return

        nodes = [n for n in pool.get_nodes() if n.is_available]
        if not nodes:
            return

        has_gpu_node = any(n.has_gpu for n in nodes)
        rm = getattr(self, "resource_manager", None)

        timeout = aiohttp.ClientTimeout(total=8.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = []
            for node in nodes:
                node_key = str(node.url)
                if rm and rm.is_model_warmed(node_key, speaker_id):
                    logger.debug(f"[Warmup] Model '{speaker_id}' already warmed on {node.summary_label}. Skipping.")
                    continue

                if provider in ("voicevox", "sharevox"):
                    style_id = voice_cfg.style_id or (3 if provider == "voicevox" else 14)
                    url = f"{node.url}/initialize_speaker?speaker={style_id}"
                    tasks.append(self._send_warmup_req(session, url, node.summary_label, voice_cfg.name, speaker_id=speaker_id, node_url=node_key))
                elif provider == "coeiroink":
                    # CPUノードかつ他にGPUノードがある場合はCPUノードへの重いダミー推論をスキップ
                    if not node.has_gpu and has_gpu_node:
                        logger.debug(f"[Warmup] Skipped COEIROINK dummy inference on CPU node {node.summary_label} (GPU available)")
                        if rm:
                            rm.mark_model_warmed(node_key, speaker_id)
                        continue

                    voice_name = getattr(voice_cfg, "voice_name", "")
                    payload = build_coeiroink_warmup_payload(voice_name)
                    if payload:
                        url = f"{node.url}/v1/synthesis"
                        tasks.append(self._send_warmup_req(session, url, node.summary_label, voice_cfg.name, json_data=payload, speaker_id=speaker_id, node_url=node_key))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_warmup_req(
        self,
        session: aiohttp.ClientSession,
        url: str,
        worker_label: str,
        spk_name: str,
        json_data: Optional[Dict[str, Any]] = None,
        speaker_id: Optional[str] = None,
        node_url: Optional[str] = None
    ) -> None:
        try:
            rm = getattr(self, "resource_manager", None)
            if json_data:
                async with session.post(url, json=json_data) as resp:
                    if resp.status == 200:
                        logger.info(f"✨ [Pre-warm] Model pre-loaded on {worker_label}: {spk_name}")
                        if rm and speaker_id and node_url:
                            rm.mark_model_warmed(node_url, speaker_id)
            else:
                async with session.post(url) as resp:
                    if resp.status in (200, 204):
                        logger.info(f"✨ [Pre-warm] Model pre-loaded on {worker_label}: {spk_name}")
                        if rm and speaker_id and node_url:
                            rm.mark_model_warmed(node_url, speaker_id)
        except Exception as e:
            logger.debug(f"Pre-warm failed on {worker_label}: {e}")

    PREVIEW_SAMPLE_TEXT = "こんにちは！超低遅延で自然な音声をお届けします。"
    PREVIEW_PITCH = "+0%"
    PREVIEW_SPEED = 1.0
    PREVIEW_USER_ID = 0

    async def ensure_preview_cached(self, speaker_id: str) -> Optional[Path]:
        """指定された話者の試聴用サンプル音声がキャッシュになければ事前生成"""
        voice_cfg = self._get_voice_config(speaker_id)
        if not voice_cfg:
            return None
        cache_key = CacheManager.generate_cache_key(
            speaker_id, self.PREVIEW_PITCH, self.PREVIEW_SPEED, self.PREVIEW_SAMPLE_TEXT, user_id=self.PREVIEW_USER_ID
        )
        cached_file = await self.cache_manager.get(cache_key, provider=voice_cfg.provider)
        if cached_file is not None and os.path.exists(cached_file):
            return cached_file
        try:
            return await self.get_audio_path(
                user_id=self.PREVIEW_USER_ID,
                speaker_id=speaker_id,
                pitch=self.PREVIEW_PITCH,
                speed=self.PREVIEW_SPEED,
                text=self.PREVIEW_SAMPLE_TEXT
            )
        except Exception as e:
            logger.debug(f"Failed to pre-cache preview for {speaker_id}: {e}")
            return None

    async def precache_all_previews(self, interval_delay: float = 0.1) -> Dict[str, int]:
        """
        全登録モデルの試聴用サンプル音声を一括事前生成（バックグラウンド実行可）
        Returns: {"total": int, "generated": int, "skipped": int, "failed": int}
        """
        voices = list(self.config.voices.items())
        total = len(voices)
        gen_cnt = 0
        skip_cnt = 0
        fail_cnt = 0
        unhealthy_providers = set()

        logger.info(f"Starting preview cache pre-generation for {total} models...")
        for spk_id, voice_cfg in voices:
            prov = getattr(voice_cfg, "provider", "")
            if prov in unhealthy_providers:
                fail_cnt += 1
                continue

            cache_key = CacheManager.generate_cache_key(
                spk_id, self.PREVIEW_PITCH, self.PREVIEW_SPEED, self.PREVIEW_SAMPLE_TEXT, user_id=self.PREVIEW_USER_ID
            )
            cached_file = await self.cache_manager.get(cache_key, provider=prov)
            if cached_file is None:
                # フォールバック先プロバイダー (Edge/Google等) のキャッシュも確認
                fb_prov = getattr(voice_cfg, "fallback_provider", "edge")
                if fb_prov and fb_prov != prov:
                    cached_file = await self.cache_manager.get(cache_key, provider=fb_prov)
            if cached_file is not None and os.path.exists(cached_file):
                skip_cnt += 1
                continue

            try:
                # ローカルエンジンの場合、必要に応じて起動確認
                if self.resource_manager and prov in self.resource_manager.LOCAL_ENGINES:
                    is_running = await self.resource_manager.is_engine_running(prov)
                    if not is_running:
                        started = await self.resource_manager.start_engine(prov, wait_healthy=True, timeout=8.0)
                        if not started:
                            unhealthy_providers.add(prov)
                            logger.warning(f"Engine '{prov}' failed to start. Skipping remaining models for this provider.")
                            fail_cnt += 1
                            continue

                await self.get_audio_path(
                    user_id=self.PREVIEW_USER_ID,
                    speaker_id=spk_id,
                    pitch=self.PREVIEW_PITCH,
                    speed=self.PREVIEW_SPEED,
                    text=self.PREVIEW_SAMPLE_TEXT
                )
                gen_cnt += 1
            except Exception as e:
                fail_cnt += 1
                logger.debug(f"Pre-cache preview failed for {spk_id}: {e}")

            if interval_delay > 0:
                await asyncio.sleep(interval_delay)

        logger.info(f"Preview cache pre-generation finished: Total={total}, Generated={gen_cnt}, Skipped={skip_cnt}, Failed={fail_cnt}")
        return {"total": total, "generated": gen_cnt, "skipped": skip_cnt, "failed": fail_cnt}

    def get_node_status(self) -> Dict[str, List[Dict[str, Any]]]:
        """全エンジンのマルチノード稼働ステータス (ヘルス、アクティブ数、レイテンシ) を取得"""
        return {
            "voicevox": self.voicevox_pool.get_status() if hasattr(self, "voicevox_pool") else [],
            "sharevox": self.sharevox_pool.get_status() if hasattr(self, "sharevox_pool") else [],
            "coeiroink": self.coeiroink_pool.get_status() if hasattr(self, "coeiroink_pool") else [],
        }
