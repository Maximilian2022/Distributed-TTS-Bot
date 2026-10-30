import os
import re
import yaml
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

@dataclass
class VoiceConfig:
    id: str
    name: str
    gender: str
    description: str
    azure_voice: str = ""
    google_voice: str = ""
    provider: str = "azure"  # "azure", "google", "voicevox", "edge"
    voice_name: str = ""     # メイン音声モデル名 (未指定時はproviderに応じてazure_voice/google_voiceを使用)
    style_id: Optional[int] = None  # VOICEVOX用スタイルID
    fallback_provider: str = "azure"
    fallback_voice: str = ""
    hidden: bool = False

    def get_primary_voice(self) -> str:
        """メインの音声モデル名を取得"""
        if self.voice_name:
            return self.voice_name
        if self.provider == "voicevox":
            return str(self.style_id or 3)
        if self.provider == "sharevox":
            return str(self.style_id or 14)
        if self.provider == "coeiroink":
            return self.voice_name or "3c37646f-3881-5374-2a83-149267990abc_0"
        if self.provider == "openjtalk":
            return self.voice_name or "ojt_mei_normal"
        if self.provider == "edge":
            return self.azure_voice or "ja-JP-NanamiNeural"
        return self.azure_voice if self.provider == "azure" else self.google_voice

    def get_fallback_voice(self) -> str:
        """フォールバック先の音声モデル名を取得"""
        if self.fallback_voice:
            return self.fallback_voice
        if self.provider in ("voicevox", "sharevox", "coeiroink", "edge", "openjtalk"):
            return "ja-JP-NanamiNeural"
        return self.google_voice if self.provider == "azure" else self.azure_voice

@dataclass
class TTSNodeConfig:
    id: str = "default"
    url: str = "http://localhost:50021"
    priority: int = 1  # 1が最高優先度 (1=GPU, 2=通常CPU, 3=バックアップ等)
    max_concurrent: int = 1  # 共用マシンへの配慮（1ノードあたりの同時リクエスト上限）
    enabled: bool = True

@dataclass
class AppConfig:
    # Bot
    command_prefix: str = "!"
    auto_join_vc_id: int = 0
    auto_join_text_channel_id: int = 0
    max_text_length: int = 50
    dedup_window_seconds: float = 5.0
    dedup_max_length: int = 10
    ignore_prefixes: List[str] = field(default_factory=lambda: [";", "；"])

    # Storage
    cache_dir: str = "/app/cache"
    db_path: str = "/app/data/bot.db"
    log_file: str = "/app/logs/bot.log"
    web_log_file: str = "/app/logs/web.log"
    engine_log_file: str = "/app/logs/engine.log"
    cache_max_size_bytes: int = 10737418240  # 10GB (Cloud)
    cache_target_size_bytes: int = 9126805504  # 8.5GB
    voicevox_cache_dir: str = "/app/cache_voicevox"
    voicevox_cache_max_size_bytes: int = 21474836480  # 20GB (VOICEVOX/SHAREVOX/COEIROINK専用)
    voicevox_cache_target_size_bytes: int = 18253611008  # 17GB
    cache_cleanup_interval_seconds: int = 300

    @property
    def data_dir(self) -> str:
        return str(Path(self.db_path).parent)

    # TTS
    tts_primary: str = "azure"
    tts_fallback: str = "google"
    speculative_timeout_seconds: float = 2.5
    # ストリーミング合成の段階導入フラグ ("off" / "shadow" / "on")
    stream_tl_speculative: str = "off"     # 投機締切を再生タイムライン基準に (最大許容沈黙)
    head_route_earliest: str = "off"       # 先頭本文パーツを完了予測最短ノードへ割当
    head_dynamic_length: str = "off"       # 先頭本文パーツ長の動的決定
    chunk_singleflight: str = "off"        # 同一パーツのローカル合成の二重発注防止
    max_silence_default_seconds: float = 1.5  # 最大許容沈黙 (遅い・止まったと感じるまで)
    az_margin_seconds: float = 0.45        # 投機先クラウドの応答マージン (Azure 実測 p95=424ms)
    head_plan_pessimism: float = 1.5       # 先頭長計画時の合成時間悲観係数
    google_monthly_limit_chars: int = 950000  # 無料枠(100万文字)の95%
    azure_region: str = "japaneast"
    azure_timeout_seconds: float = 5.0
    google_endpoint: str = "asia-northeast1-texttospeech.googleapis.com"
    google_timeout_seconds: float = 5.0
    voicevox_base_url: str = "http://voicevox:50021"
    voicevox_timeout_seconds: float = 10.0
    voicevox_default_fallback_style_id: int = 3
    sharevox_base_url: str = "http://sharevox:50025"
    sharevox_timeout_seconds: float = 10.0
    coeiroink_base_url: str = "http://coeiroink:50031"
    coeiroink_timeout_seconds: float = 25.0
    openjtalk_voices_dir: str = "/app/openjtalk_voices"
    openjtalk_dic_dir: str = "/var/lib/mecab/dic/open-jtalk/naist-jdic"
    openjtalk_timeout_seconds: float = 5.0

    # Multi-node / Compute Node Settings
    voicevox_nodes: List[TTSNodeConfig] = field(default_factory=list)
    sharevox_nodes: List[TTSNodeConfig] = field(default_factory=list)
    coeiroink_nodes: List[TTSNodeConfig] = field(default_factory=list)

    # Web UI & OAuth2
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    discord_client_id: str = ""
    discord_client_secret: str = ""
    discord_redirect_uri: str = "http://localhost:8088/callback"
    web_dashboard_url: str = "http://localhost:8088"
    web_session_secret: str = ""
    worker_shared_secret: str = ""

    # Voice defaults
    default_speaker: str = "nanami"
    default_pitch: str = "0%"
    default_speed: float = 1.0

    # Voices map
    voices: Dict[str, VoiceConfig] = field(default_factory=dict)

    # API Keys / Credentials (1〜10号機Botトークン辞書)
    discord_token: str = ""
    discord_tokens_map: Dict[int, str] = field(default_factory=dict)
    azure_speech_key: str = ""
    azure_speech_region: str = "japaneast"
    google_application_credentials: str = ""
    google_api_key: str = ""
    google_credentials_json: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    admin_password: str = ""
    ha_secret: str = ""
    cloudflare_tunnel_token: str = ""

    def get_discord_tokens(self) -> List[Tuple[int, str]]:
        """有効な全Botトークンを (bot_index, token) の昇順リストで取得 (最大10台対応)"""
        tokens = []
        # 1. 辞書から取得
        for idx in range(1, 11):
            tok = self.discord_tokens_map.get(idx) or (self.discord_token if idx == 1 else "")
            if tok and tok.strip():
                tokens.append((idx, tok.strip()))
        return tokens

    def apply_db_credentials(self, credentials: Dict[str, str], bot_tokens: List[Dict[str, Any]]) -> None:
        """データベースに保存された最新の認証情報・トークンを AppConfig に適用"""
        if credentials.get("admin_password"):
            self.admin_password = credentials["admin_password"]
        if credentials.get("azure_speech_key"):
            self.azure_speech_key = credentials["azure_speech_key"]
        if credentials.get("azure_speech_region"):
            self.azure_speech_region = credentials["azure_speech_region"]
        if credentials.get("google_api_key"):
            self.google_api_key = credentials["google_api_key"]
        if credentials.get("google_credentials_json"):
            self.google_credentials_json = credentials["google_credentials_json"]
        if credentials.get("worker_shared_secret"):
            self.worker_shared_secret = credentials["worker_shared_secret"]
        if credentials.get("ha_secret"):
            self.ha_secret = credentials["ha_secret"]
        if credentials.get("cloudflare_tunnel_token") is not None:
            self.cloudflare_tunnel_token = credentials["cloudflare_tunnel_token"]
        if credentials.get("web_base_url"):
            base = credentials["web_base_url"].strip().rstrip("/")
            self.web_dashboard_url = base
            self.discord_redirect_uri = f"{base}/callback"

        # トークン辞書の更新
        for bt in bot_tokens:
            idx = int(bt.get("bot_index", 1))
            if bt.get("is_enabled", 1) and bt.get("token"):
                self.discord_tokens_map[idx] = bt["token"].strip()
                if idx == 1:
                    self.discord_token = bt["token"].strip()
            elif not bt.get("is_enabled", 1):
                self.discord_tokens_map.pop(idx, None)
                if idx == 1:
                    self.discord_token = ""

    def reload(self, config_path: str = "config.yaml") -> None:
        """YAMLファイルおよび.envから最新の設定をロードして現在のインスタンスに反映"""
        env_path = Path(".env")
        if env_path.exists():
            try:
                with open(env_path, "r", encoding="utf-8") as ef:
                    for line in ef:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip("'\"")
                            os.environ[k] = v
            except Exception:
                pass

        new_cfg = load_config(config_path)
        for attr, value in new_cfg.__dict__.items():
            setattr(self, attr, value)

ROLLOUT_MODES = ("off", "shadow", "on")


def normalize_rollout_mode(value: Any) -> str:
    """段階導入フラグ値を off / shadow / on に正規化 (不明値は安全側の off)"""
    v = str(value or "off").strip().lower()
    if v in ("true", "1", "yes"):
        return "on"
    return v if v in ROLLOUT_MODES else "off"


def load_config(config_path: str = "config.yaml") -> AppConfig:
    cfg = AppConfig()

    # .env ファイルが存在する場合、未設定の環境変数を自動補完
    env_path = Path(".env")
    if env_path.exists():
        try:
            with open(env_path, "r", encoding="utf-8") as ef:
                for line in ef:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

    path = Path(config_path)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        bot_raw = raw.get("bot", {})
        cfg.command_prefix = bot_raw.get("command_prefix", cfg.command_prefix)
        cfg.auto_join_vc_id = int(bot_raw.get("auto_join_vc_id", cfg.auto_join_vc_id))
        cfg.auto_join_text_channel_id = int(bot_raw.get("auto_join_text_channel_id", cfg.auto_join_text_channel_id))
        cfg.max_text_length = int(bot_raw.get("max_text_length", cfg.max_text_length))
        cfg.dedup_window_seconds = float(bot_raw.get("dedup_window_seconds", cfg.dedup_window_seconds))
        cfg.dedup_max_length = int(bot_raw.get("dedup_max_length", cfg.dedup_max_length))
        raw_prefixes = bot_raw.get("ignore_prefixes")
        if raw_prefixes is not None:
            if isinstance(raw_prefixes, list):
                cfg.ignore_prefixes = [str(p) for p in raw_prefixes if str(p).strip()]
            elif isinstance(raw_prefixes, str):
                cfg.ignore_prefixes = [p.strip() for p in re.split(r'[,、\s]+', raw_prefixes) if p.strip()]

        storage_raw = raw.get("storage", {})
        cfg.cache_dir = storage_raw.get("cache_dir", cfg.cache_dir)
        cfg.db_path = storage_raw.get("db_path", cfg.db_path)
        cfg.log_file = storage_raw.get("log_file", cfg.log_file)
        cfg.web_log_file = storage_raw.get("web_log_file", cfg.web_log_file)
        cfg.cache_max_size_bytes = int(storage_raw.get("cache_max_size_bytes", cfg.cache_max_size_bytes))
        cfg.cache_target_size_bytes = int(storage_raw.get("cache_target_size_bytes", cfg.cache_target_size_bytes))
        cfg.voicevox_cache_dir = storage_raw.get("voicevox_cache_dir", cfg.voicevox_cache_dir)
        cfg.voicevox_cache_max_size_bytes = int(storage_raw.get("voicevox_cache_max_size_bytes", cfg.voicevox_cache_max_size_bytes))
        cfg.voicevox_cache_target_size_bytes = int(storage_raw.get("voicevox_cache_target_size_bytes", cfg.voicevox_cache_target_size_bytes))
        cfg.cache_cleanup_interval_seconds = int(storage_raw.get("cache_cleanup_interval_seconds", cfg.cache_cleanup_interval_seconds))

        tts_raw = raw.get("tts", {})
        cfg.tts_primary = tts_raw.get("primary", cfg.tts_primary)
        cfg.tts_fallback = tts_raw.get("fallback", cfg.tts_fallback)
        cfg.speculative_timeout_seconds = float(tts_raw.get("speculative_timeout_seconds", cfg.speculative_timeout_seconds))
        stream_raw = tts_raw.get("streaming", {}) or {}
        for _flag in ("stream_tl_speculative", "head_route_earliest", "head_dynamic_length", "chunk_singleflight"):
            setattr(cfg, _flag, normalize_rollout_mode(stream_raw.get(_flag, getattr(cfg, _flag))))
        cfg.max_silence_default_seconds = float(stream_raw.get("max_silence_default_seconds", cfg.max_silence_default_seconds))
        cfg.az_margin_seconds = float(stream_raw.get("az_margin_seconds", cfg.az_margin_seconds))
        cfg.head_plan_pessimism = float(stream_raw.get("head_plan_pessimism", cfg.head_plan_pessimism))
        cfg.google_monthly_limit_chars = int(tts_raw.get("google_monthly_limit_chars", cfg.google_monthly_limit_chars))
        azure_raw = tts_raw.get("azure", {})
        cfg.azure_region = azure_raw.get("region", cfg.azure_region)
        cfg.azure_timeout_seconds = float(azure_raw.get("timeout_seconds", cfg.azure_timeout_seconds))
        google_raw = tts_raw.get("google", {})
        cfg.google_endpoint = google_raw.get("endpoint", cfg.google_endpoint)
        cfg.google_timeout_seconds = float(google_raw.get("timeout_seconds", cfg.google_timeout_seconds))
        voicevox_raw = tts_raw.get("voicevox", {})
        cfg.voicevox_base_url = voicevox_raw.get("base_url", cfg.voicevox_base_url)
        cfg.voicevox_timeout_seconds = float(voicevox_raw.get("timeout_seconds", cfg.voicevox_timeout_seconds))
        cfg.voicevox_default_fallback_style_id = int(voicevox_raw.get("default_fallback_style_id", cfg.voicevox_default_fallback_style_id))
        sharevox_raw = tts_raw.get("sharevox", {})
        cfg.sharevox_base_url = sharevox_raw.get("base_url", cfg.sharevox_base_url)
        cfg.sharevox_timeout_seconds = float(sharevox_raw.get("timeout_seconds", cfg.sharevox_timeout_seconds))
        coeiroink_raw = tts_raw.get("coeiroink", {})
        cfg.coeiroink_base_url = coeiroink_raw.get("base_url", cfg.coeiroink_base_url)
        cfg.coeiroink_timeout_seconds = float(coeiroink_raw.get("timeout_seconds", cfg.coeiroink_timeout_seconds))
        openjtalk_raw = tts_raw.get("openjtalk", {})
        cfg.openjtalk_voices_dir = openjtalk_raw.get("voices_dir", cfg.openjtalk_voices_dir)
        cfg.openjtalk_dic_dir = openjtalk_raw.get("dic_dir", cfg.openjtalk_dic_dir)
        cfg.openjtalk_timeout_seconds = float(openjtalk_raw.get("timeout_seconds", cfg.openjtalk_timeout_seconds))

        # マルチノード (Compute Nodes) パース & 従来互換フォールバック
        def _parse_nodes(raw_section: Dict[str, Any], default_url: str, default_port: int, default_id: str) -> List[TTSNodeConfig]:
            nodes_list = raw_section.get("nodes", [])
            if nodes_list and isinstance(nodes_list, list):
                result = []
                for i, n in enumerate(nodes_list):
                    if isinstance(n, dict):
                        result.append(TTSNodeConfig(
                            id=str(n.get("id", f"{default_id}_{i+1}")),
                            url=str(n.get("url", default_url)),
                            priority=int(n.get("priority", 1)),
                            max_concurrent=int(n.get("max_concurrent", 1)),
                            enabled=bool(n.get("enabled", True))
                        ))
                if result:
                    return result
            # 未設定または空配列の場合は単一base_urlで初期化 (従来互換)
            return [TTSNodeConfig(id=default_id, url=default_url, priority=1, max_concurrent=1, enabled=True)]

        cfg.voicevox_nodes = _parse_nodes(voicevox_raw, cfg.voicevox_base_url, 50021, "local_master")
        cfg.sharevox_nodes = _parse_nodes(sharevox_raw, cfg.sharevox_base_url, 50025, "local_master")
        cfg.coeiroink_nodes = _parse_nodes(coeiroink_raw, cfg.coeiroink_base_url, 50031, "local_master")

        cfg.default_speaker = raw.get("default_speaker", cfg.default_speaker)
        cfg.default_pitch = raw.get("default_pitch", cfg.default_pitch)
        cfg.default_speed = float(raw.get("default_speed", cfg.default_speed))

        voices_raw = raw.get("voices", {})
        for v_id, v_data in voices_raw.items():
            provider = v_data.get("provider", "azure")
            fallback_provider = v_data.get("fallback_provider", "google" if provider == "azure" else "azure")
            cfg.voices[v_id] = VoiceConfig(
                id=v_id,
                name=v_data.get("name", v_id),
                gender=v_data.get("gender", "unknown"),
                description=v_data.get("description", ""),
                azure_voice=v_data.get("azure_voice", ""),
                google_voice=v_data.get("google_voice", ""),
                provider=provider,
                voice_name=v_data.get("voice_name", ""),
                fallback_provider=fallback_provider,
                fallback_voice=v_data.get("fallback_voice", "")
            )

    # 環境変数からの上書き (1〜10号機Botトークン)
    cfg.discord_token = os.getenv("DISCORD_TOKEN", cfg.discord_token).strip()
    if cfg.discord_token:
        cfg.discord_tokens_map[1] = cfg.discord_token
    for i in range(2, 11):
        tok = os.getenv(f"DISCORD_TOKEN_{i}", "").strip()
        if tok:
            cfg.discord_tokens_map[i] = tok

    cfg.azure_speech_key = os.getenv("AZURE_SPEECH_KEY", cfg.azure_speech_key)
    cfg.azure_speech_region = os.getenv("AZURE_SPEECH_REGION", cfg.azure_region)
    cfg.google_application_credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", cfg.google_application_credentials)
    cfg.google_api_key = os.getenv("GOOGLE_API_KEY", cfg.google_api_key)
    cfg.google_credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON", cfg.google_credentials_json)
    cfg.voicevox_base_url = os.getenv("VOICEVOX_URL", cfg.voicevox_base_url)
    cfg.sharevox_base_url = os.getenv("SHAREVOX_URL", cfg.sharevox_base_url)
    cfg.coeiroink_base_url = os.getenv("COEIROINK_URL", cfg.coeiroink_base_url)
    cfg.gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    cfg.gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
    cfg.speculative_timeout_seconds = float(os.getenv("SPECULATIVE_TIMEOUT_SECONDS", cfg.speculative_timeout_seconds))
    for _flag in ("stream_tl_speculative", "head_route_earliest", "head_dynamic_length", "chunk_singleflight"):
        setattr(cfg, _flag, normalize_rollout_mode(os.getenv(_flag.upper(), getattr(cfg, _flag))))
    cfg.cloudflare_tunnel_token = os.getenv("CLOUDFLARE_TUNNEL_TOKEN", cfg.cloudflare_tunnel_token).strip()

    # Web UI & OAuth2 環境変数 (汎用WEB_BASE_URL対応)
    cfg.admin_password = os.getenv("ADMIN_PASSWORD", cfg.admin_password).strip()
    cfg.web_port = int(os.getenv("WEB_PORT", cfg.web_port))
    cfg.web_host = os.getenv("WEB_HOST", cfg.web_host)
    cfg.discord_client_id = os.getenv("DISCORD_CLIENT_ID", cfg.discord_client_id).strip()
    cfg.discord_client_secret = os.getenv("DISCORD_CLIENT_SECRET", cfg.discord_client_secret).strip()

    base_url = os.getenv("WEB_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        base_url = os.getenv("WEB_DASHBOARD_URL", "").strip().rstrip("/")
    if not base_url:
        public_port = os.getenv("HOST_WEB_PORT", "8088")
        base_url = f"http://localhost:{public_port}"
    cfg.web_dashboard_url = base_url
    cfg.discord_redirect_uri = os.getenv("DISCORD_REDIRECT_URI", f"{base_url}/callback").strip()

    cfg.web_session_secret = os.getenv("WEB_SESSION_SECRET", cfg.web_session_secret) or os.getenv("SESSION_SECRET", "")
    if not cfg.web_session_secret:
        cfg.web_session_secret = secrets.token_hex(32)

    cfg.worker_shared_secret = os.getenv("WORKER_SHARED_SECRET", "").strip()
    if not cfg.worker_shared_secret:
        cfg.worker_shared_secret = secrets.token_hex(16)

    cfg.ha_secret = os.getenv("TTSBOT_HA_SECRET", os.getenv("HA_SECRET", "")).strip()

    # ホスト直接実行時にもローカルディレクトリが存在するように作成
    for directory in [Path(cfg.cache_dir), Path(cfg.db_path).parent, Path(cfg.log_file).parent]:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    return cfg

def save_yaml_config(cfg: AppConfig, config_path: str = "config.yaml") -> None:
    """WebUI等から変更されたボット設定をYAMLファイルに永続化"""
    path = Path(config_path)
    existing_raw = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing_raw = yaml.safe_load(f) or {}
        except Exception:
            existing_raw = {}

    bot_dict = existing_raw.get("bot", {})
    bot_dict["command_prefix"] = cfg.command_prefix
    bot_dict["auto_join_vc_id"] = cfg.auto_join_vc_id
    bot_dict["auto_join_text_channel_id"] = cfg.auto_join_text_channel_id
    bot_dict["max_text_length"] = cfg.max_text_length
    bot_dict["dedup_window_seconds"] = cfg.dedup_window_seconds
    bot_dict["dedup_max_length"] = cfg.dedup_max_length
    existing_raw["bot"] = bot_dict

    existing_raw["default_speaker"] = cfg.default_speaker
    existing_raw["default_pitch"] = cfg.default_pitch
    existing_raw["default_speed"] = cfg.default_speed

    tts_dict = existing_raw.get("tts", {})
    tts_dict["speculative_timeout_seconds"] = cfg.speculative_timeout_seconds
    existing_raw["tts"] = tts_dict

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(existing_raw, f, allow_unicode=True, default_flow_style=False)

def reload_config(cfg: AppConfig, config_path: str = "config.yaml") -> AppConfig:
    """設定インスタンスをホットリロード"""
    cfg.reload(config_path)
    return cfg
