"""
マルチノード / 分散コンピュートワーカー管理プール (TTSNodePool)
- 共用マシンへの配慮: 各ノードごとに max_concurrent (同時実行数上限) を厳格に制限
- 動的スペック適応: WorkerのGPU/CPU/RAMスペックに応じた優先度・並列数の動的調整
- 動的自動登録 & ハートビート監視: Workerからの自動接続と死活監視 (オフライン時自動切り離し)
"""

import re
import json
import asyncio
import time
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any
import aiohttp
from src.config import TTSNodeConfig
from src.tts.base import TTSConnectionError, TTSError
from src.utils.latency_profiler import latency_profiler

logger = logging.getLogger(__name__)

# 動的Tierルーティング設定 (デフォルト: GPU+RAMDisk, CPU+RAMDisk, GPU+SSD, CPU+SSD)
DEFAULT_TIER_ROUTING_ORDER: List[str] = ["gpu_ram", "cpu_ram", "gpu_ssd", "cpu_ssd"]

# エンジン別設定 (未設定時は default を参照)
_tier_routing_by_engine: Dict[str, List[str]] = {
    "default": list(DEFAULT_TIER_ROUTING_ORDER),
    "coeiroink": list(DEFAULT_TIER_ROUTING_ORDER),
    "voicevox": list(DEFAULT_TIER_ROUTING_ORDER),
    "sharevox": list(DEFAULT_TIER_ROUTING_ORDER),
}

TIER_METADATA: Dict[str, Dict[str, str]] = {
    "gpu_ram": {
        "name": "GPU + RAMDisk",
        "description": "GPU高速推論 & メモリ常駐 (最速・即時応答)",
        "badge": "最速",
        "badge_color": "#00ffcc"
    },
    "cpu_ram": {
        "name": "CPU + RAMDisk",
        "description": "CPU高速推論 & メモリ常駐 (GPU温存・即時応答)",
        "badge": "高速",
        "badge_color": "#5865f2"
    },
    "gpu_ssd": {
        "name": "GPU + SSD",
        "description": "GPU高速推論 & SSDモデル (中速・大容量モデル)",
        "badge": "中速",
        "badge_color": "#f39c12"
    },
    "cpu_ssd": {
        "name": "CPU + SSD",
        "description": "汎用CPU & SSDモデル (低速フォールバック)",
        "badge": "最小",
        "badge_color": "#95a5a6"
    }
}

SUPPORTED_ROUTING_ENGINES = [
    {"id": "default", "name": "共通デフォルト", "icon": "🌐"},
    {"id": "coeiroink", "name": "COEIROINK", "icon": "🎨"},
    {"id": "voicevox", "name": "VOICEVOX", "icon": "🔊"},
    {"id": "sharevox", "name": "SHAREVOX", "icon": "📢"},
]

TIER_ROUTING_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "tier_routing.json"

def get_tier_routing_order(engine: Optional[str] = None) -> List[str]:
    """指定エンジン（またはデフォルト）のTierルーティング優先順位を取得"""
    global _tier_routing_by_engine
    if engine:
        eng_clean = str(engine).strip().lower()
        if eng_clean in _tier_routing_by_engine:
            return list(_tier_routing_by_engine[eng_clean])
    return list(_tier_routing_by_engine.get("default", DEFAULT_TIER_ROUTING_ORDER))

def get_all_tier_routing_orders() -> Dict[str, List[str]]:
    """全エンジンのTierルーティング優先順位を取得"""
    global _tier_routing_by_engine
    return {k: list(v) for k, v in _tier_routing_by_engine.items()}

def set_tier_routing_order(engine: str, order: List[str]) -> bool:
    """指定エンジンのTierルーティング優先順位を検証して設定"""
    global _tier_routing_by_engine
    valid_keys = {"gpu_ram", "cpu_ram", "gpu_ssd", "cpu_ssd"}
    if isinstance(order, list) and len(order) == 4 and set(order) == valid_keys:
        eng_clean = str(engine).strip().lower()
        _tier_routing_by_engine[eng_clean] = list(order)
        return True
    return False

def load_tier_routing_from_disk() -> None:
    """ディスクからTierルーティング設定をロード"""
    global _tier_routing_by_engine
    try:
        if TIER_ROUTING_CONFIG_PATH.exists():
            data = json.loads(TIER_ROUTING_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                engines = data.get("engines")
                if isinstance(engines, dict):
                    for eng, ord_list in engines.items():
                        set_tier_routing_order(eng, ord_list)
                single_order = data.get("tier_order")
                if single_order and isinstance(single_order, list):
                    set_tier_routing_order("default", single_order)
            logger.info(f"Loaded dynamic tier routing orders from disk: {_tier_routing_by_engine}")
    except Exception as e:
        logger.warning(f"Failed to load tier routing order from disk: {e}")

def save_tier_routing_to_disk(engine_orders: Optional[Dict[str, List[str]]] = None) -> bool:
    """Tierルーティング設定をディスクに永続化保存"""
    global _tier_routing_by_engine
    if engine_orders:
        for eng, ord_list in engine_orders.items():
            set_tier_routing_order(eng, ord_list)
    try:
        TIER_ROUTING_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "engines": get_all_tier_routing_orders(),
            "tier_order": get_tier_routing_order("default")
        }
        TIER_ROUTING_CONFIG_PATH.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        logger.info(f"Saved dynamic tier routing orders to disk: {payload}")
        return True
    except Exception as e:
        logger.error(f"Failed to save tier routing order: {e}")
        return False

# モジュールインポート時にディスクから最新設定を読み込み
load_tier_routing_from_disk()

_HEX12_RE = re.compile(r"^[0-9a-fA-F]{12}$")

# COEIROINK キャラクターUUID <-> モデルディレクトリ名（小文字）相互マッピング辞書
COEIROINK_MODEL_MAPPING: Dict[str, str] = {
    "b28bb401-bc43-c9c7-77e4-77a2bbb4b283": "dia-2.1.0",
    "dia-2.1.0": "b28bb401-bc43-c9c7-77e4-77a2bbb4b283",
    "a60ebf6c-626a-7ce6-5d69-c92bf2a1a1d0": "ofutonp-2.1.1",
    "ofutonp-2.1.1": "a60ebf6c-626a-7ce6-5d69-c92bf2a1a1d0",
    "f8be6104-aa0e-11f1-b3bf-0242ac1c000c": "f8be6104-aa0e-11f1-b3bf-0242ac1c000c",
    "297a5b91-f88a-6951-5841-f1e648b2e594": "kana-2.0.0",
    "kana-2.0.0": "297a5b91-f88a-6951-5841-f1e648b2e594",
    "d41bcbd9-f4a9-4e10-b000-7a431568dd01": "kanae-2.1.0",
    "kanae-2.1.0": "d41bcbd9-f4a9-4e10-b000-7a431568dd01",
    "d312d0fb-d38d-434e-825d-cbcbfd105ad0": "ginga-2.2.0",
    "ginga-2.2.0": "d312d0fb-d38d-434e-825d-cbcbfd105ad0",
    "292ea286-3d5f-f1cc-157c-66462a6a9d08": "mana-2.1.0",
    "mana-2.1.0": "292ea286-3d5f-f1cc-157c-66462a6a9d08",
    "d31b5442-dddb-11ec-82ba-0242ac1c0002": "d31b5442-dddb-11ec-82ba-0242ac1c0002",
    "d219f5ab-a50b-4d99-a26a-a9fc213e9100": "aoba-2.0.1",
    "aoba-2.0.1": "d219f5ab-a50b-4d99-a26a-a9fc213e9100",
    "cc1153b4-d20c-46dd-a308-73ca38c0e85a": "croix-2.1.0",
    "croix-2.1.0": "cc1153b4-d20c-46dd-a308-73ca38c0e85a",
    "cc213e6d-d847-45b5-a1df-415744c890f2": "andzie-2.0.0",
    "andzie-2.0.0": "cc213e6d-d847-45b5-a1df-415744c890f2",
    "c97966b1-d80c-04f5-aba5-d30a92843b59": "arma-2.0.0",
    "arma-2.0.0": "c97966b1-d80c-04f5-aba5-d30a92843b59",
    "cb11bdbd-78fc-4f16-b528-a400bae1782d": "lilin-2.1.0",
    "lilin-2.1.0": "cb11bdbd-78fc-4f16-b528-a400bae1782d",
    "d1143ac1-c486-4273-92ef-a30938d01b91": "ayaka-2.0.2",
    "ayaka-2.0.2": "d1143ac1-c486-4273-92ef-a30938d01b91",
    "3c37646f-3881-5374-2a83-149267990abc": "tsukuyomichan-2.0.0",
    "tsukuyomichan-2.0.0": "3c37646f-3881-5374-2a83-149267990abc",
    "d93140ec-d365-11ec-8f1d-0242ac1c0002": "amitaro-10.0",
    "amitaro-10.0": "d93140ec-d365-11ec-8f1d-0242ac1c0002",
    "cc31327e-6617-446f-ba41-eb88f89b8fce": "ameno-2.0.0",
    "ameno-2.0.0": "cc31327e-6617-446f-ba41-eb88f89b8fce"
}



# 更新/再起動のため新規割り当てを止めているワーカーホスト(IP)集合 (ローリング更新・自動更新用)
DRAINING_HOSTS: set = set()


class TTSNode:
    """単一の TTS エンジンノード（コンピュートワーカー）を管理"""

    def __init__(self, config: TTSNodeConfig, specs: Optional[Dict[str, Any]] = None, hostname: str = ""):
        self.config = config
        self.id = config.id
        self.url = config.url.rstrip("/")
        self.priority = config.priority
        self.max_concurrent = max(1, config.max_concurrent)
        self.enabled = config.enabled
        self.specs = specs or {}
        self.hostname = hostname
        self.engine_running = bool(self.specs.get("engine_running", True)) if "engine_running" in self.specs else True

        self.semaphore = asyncio.Semaphore(self.max_concurrent)
        self.active_count = 0
        self.is_healthy = True
        self.unhealthy_until = 0.0
        self.last_heartbeat = time.time()
        self.total_requests = 0
        self.failed_requests = 0
        self.last_latency_ms = 0.0
        self.last_start_attempt = 0.0
        self._session: Optional[aiohttp.ClientSession] = None
        try:
            from urllib.parse import urlparse
            self._host = urlparse(self.url).hostname or ""
        except Exception:
            self._host = ""

    async def _get_agent_session(self) -> aiohttp.ClientSession:
        """永続セッションを取得 (Keep-Aliveコネクション再利用でTCPハンドシェイク遅延を排除)"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=8.0, connect=3.0)
            connector = aiohttp.TCPConnector(limit=5, keepalive_timeout=60.0)
            self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        return self._session

    async def close(self) -> None:
        """ノードに紐づくHTTPセッション等のリソースを安全に解放"""
        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    @property
    def draining(self) -> bool:
        """このノードのホストが更新/再起動のため割り当て停止(ドレイン)中か"""
        return bool(self._host) and self._host in DRAINING_HOSTS

    async def try_remote_start(self) -> bool:
        """ワーカーのAgentに対してエンジンのオンデマンド遠隔起動を試みる (GPU・CPU・自ホスト問わず自律復帰)"""
        if not self.url:
            return False
        try:
            from urllib.parse import urlparse
            p = urlparse(self.url)
            ip = p.hostname
            if not ip:
                return False
            # localhost/127.0.0.1 の場合もローカルAgentポート50030へ接続
            target_ip = "127.0.0.1" if ip in ("localhost", "127.0.0.1") else ip
            engine = self.engine_name.lower()
            agent_url = f"http://{target_ip}:50030/engine/start"
            logger.info(f"⚡ [On-Demand Start] Sending remote start for '{engine}' to worker ({target_ip}:50030)...")
            session = await self._get_agent_session()
            async with session.post(agent_url, json={"engine": engine}) as resp:
                if resp.status == 200:
                    logger.info(f"✨ [On-Demand Start] Successfully woke engine '{engine}' on worker ({target_ip})")
                    self.is_healthy = True
                    self.engine_running = True
                    self.specs["engine_running"] = True
                    self.unhealthy_until = 0.0
                    return True
        except Exception as e:
            logger.debug(f"Could not remote-start engine on worker {self.id}: {e}")
        return False

    @property
    def machine_name(self) -> str:
        """マシン名（簡潔なホスト名とGPU/CPU情報）"""
        host = self.hostname or self.id
        clean = host
        for prefix in ("worker_", "worker-"):
            if clean.lower().startswith(prefix):
                clean = clean[len(prefix):]
        for suffix in ("_voicevox", "_sharevox", "_coeiroink"):
            if clean.lower().endswith(suffix):
                clean = clean[:-len(suffix)]
        # specs にホスト名が明示されている場合は最優先 (HOST_HOSTNAME 連携)
        explicit_host = self.specs.get("host_hostname") or self.specs.get("hostname") or self.specs.get("machine_name")
        if explicit_host:
            clean = str(explicit_host).strip()

        if clean == "local_master":
            return "Master (CPU)"

        # コンテナID (12文字Hex) の場合は specs または GPU / OS 名から動的解決
        if _HEX12_RE.match(clean):
            if explicit_host:
                clean = explicit_host
            else:
                gpu_name = str(self.specs.get("gpu_name", "")).strip()
                os_name = str(self.specs.get("os", "")).strip()
                if gpu_name:
                    clean_gpu = gpu_name.replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")
                    clean = f"Worker ({clean_gpu})"
                elif os_name:
                    clean = f"Worker ({os_name})"
                else:
                    clean = f"Worker ({clean[:6]})"

        if self.specs.get("gpu"):
            gpu_name = self.specs.get("gpu_name", "GPU")
            gpu_name = gpu_name.replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")
            return f"{clean} ({gpu_name})"
        return f"{clean} (CPU)"

    @property
    def engine_name(self) -> str:
        """担当エンジン名（ポートやURLから判定）"""
        if ":50021" in self.url:
            return "VOICEVOX"
        elif ":50025" in self.url:
            return "SHAREVOX"
        elif ":50031" in self.url:
            return "COEIROINK"
        return "LocalEngine"

    @property
    def short_label(self) -> str:
        """UI表示用のスマートバッジ文字列 (例: 'GPU-Worker (RTX 3090) / VOICEVOX')"""
        return f"{self.machine_name} / {self.engine_name}"

    @property
    def summary_label(self) -> str:
        """ログ・追跡用ノード識別文字列 (スマート表示)"""
        return f"{self.machine_name} / {self.engine_name}"

    @property
    def has_gpu(self) -> bool:
        """ノードがGPUを搭載しているか判定"""
        return bool(self.specs.get("gpu", False))

    @property
    def is_external(self) -> bool:
        """外部Workerノードか判定 (local_master以外)"""
        return self.id != "local_master" and not self.url.startswith("http://localhost") and not self.url.startswith("http://127.0.0.1")

    @property
    def is_available(self) -> bool:
        """ノードが現在リクエスト受付可能か判定"""
        if not self.enabled:
            return False
        if self.draining:
            return False
        if not self.is_healthy:
            return False
        if time.time() < self.unhealthy_until:
            return False
        # エンジンが明示的に停止中 (False) と報告されている場合はリクエスト不可
        if self.engine_running is False or self.specs.get("engine_running") is False:
            return False
        # ワーカー側でヘルスチェック異常 (False) の場合も除外
        if self.specs.get("engine_healthy") is False:
            return False
        return True


    def has_model_in_ram(self, model_name: Optional[str]) -> bool:
        """指定モデル（UUIDやディレクトリ名）がノードのRAMDiskに常駐しているか判定"""
        if not model_name:
            return False
        ram_models = self.specs.get("ram_models", [])
        if not ram_models:
            return False

        # model_name は "d312d0fb-d38d-434e-825d-cbcbfd105ad0_72" や "ginga-2.2.0" などの形式
        raw = str(model_name).strip().lower()
        uuid_or_key = raw.split("_")[0].split(":")[0].strip()

        # 照合候補のセットを作成（元の文字列、アンダースコア前、およびマッピング先の名前/UUID）
        candidates = {raw, uuid_or_key}
        mapped = COEIROINK_MODEL_MAPPING.get(uuid_or_key) or COEIROINK_MODEL_MAPPING.get(raw)
        if mapped:
            candidates.add(mapped.lower())
            mapped_short = mapped.split("-")[0].lower()
            candidates.add(mapped_short)

        if "-" in uuid_or_key:
            candidates.add(uuid_or_key.split("-")[0])

        for rm in ram_models:
            rm_lower = str(rm).strip().lower()
            rm_short = rm_lower.split("-")[0]
            rm_mapped = COEIROINK_MODEL_MAPPING.get(rm_lower, "").lower()

            for cand in candidates:
                if (
                    cand == rm_lower
                    or cand == rm_mapped
                    or cand == rm_short
                    or (len(cand) >= 4 and cand in rm_lower)
                    or (len(rm_short) >= 4 and rm_short in cand)
                ):
                    return True
        return False

    def supports_model(self, engine: str, model_name: Optional[str]) -> bool:
        """指定されたモデル（話者UUID等）をこのノードがサポートしているか"""
        return True

    def mark_success(self, latency_ms: float = 0.0):
        """リクエスト成功時の状態更新"""
        self.is_healthy = True
        self.unhealthy_until = 0.0
        self.last_heartbeat = time.time()
        if latency_ms > 0:
            self.last_latency_ms = latency_ms

    def mark_failed(self, cooldown_seconds: float = 25.0, reason: str = "", details: Optional[str] = None):
        """リクエスト失敗・タイムアウト時の自動隔離（クールダウン）と管理者DM通知"""
        now = time.time()
        was_healthy = self.is_healthy and (now >= self.unhealthy_until)
        self.failed_requests += 1
        self.is_healthy = False
        self.unhealthy_until = max(self.unhealthy_until, now + cooldown_seconds)
        logger.warning(
            f"⚠️ [NodePool] Node '{self.id}' ({self.url}) failed ({reason}). "
            f"Marked unhealthy for {cooldown_seconds:.0f}s."
        )
        if not was_healthy:
            # 既に隔離中のノードに対する連続エラー時は、多重通知を抑止
            return
        try:
            from src.utils.alert_service import (
                alert_service,
                CATEGORY_WORKER_ENGINE,
                CATEGORY_MASTER_ENGINE,
                SEVERITY_ERROR
            )
            cat = CATEGORY_WORKER_ENGINE if self.is_external else CATEGORY_MASTER_ENGINE
            alert_service.notify(
                title=f"{self.engine_name} 推論失敗・一時隔離",
                message=f"ノード `{self.machine_name}` 上の `{self.engine_name}` が応答に失敗したため、一時隔離されました ({cooldown_seconds:.0f}秒)。\n**原因**: {reason}",
                category=cat,
                severity=SEVERITY_ERROR,
                node_name=self.machine_name,
                engine_name=self.engine_name,
                details=details
            )
        except Exception as e:
            logger.debug(f"Alert notify error in mark_failed: {e}")

    def update_heartbeat(self):
        """ハートビート受信時の生存更新"""
        self.last_heartbeat = time.time()
        if not self.is_healthy and self.unhealthy_until <= time.time():
            self.is_healthy = True
            if self.specs.get("engine_running") is not False:
                logger.info(f"💚 [NodePool] Node '{self.id}' ({self.url}) recovered via heartbeat.")

    def to_dict(self) -> Dict[str, Any]:
        """WebUI / ログ用ステータス辞書"""
        total = self.total_requests
        failed = self.failed_requests
        success = max(0, total - failed)
        success_rate = round((success / total) * 100.0, 1) if total > 0 else 100.0

        now_ts = time.time()
        hb_ago = round(now_ts - self.last_heartbeat, 1)
        # Agentの死活判定: 直近60秒以内にハートビート受信があるか (ローカルマスターは常時True)
        agent_online = True if not self.is_external else (hb_ago < 60.0)
        # エンジンコンテナの稼働状況
        engine_running = (self.engine_running is not False) and (self.specs.get("engine_running", True) is not False)
        engine_healthy = self.specs.get("engine_healthy", True) is not False

        return {
            "id": self.id,
            "url": self.url,
            "priority": self.priority,
            "max_concurrent": self.max_concurrent,
            "active_count": self.active_count,
            "enabled": self.enabled,
            "draining": self.draining,
            "agent_online": agent_online,
            "engine_running": engine_running,
            "engine_healthy": engine_healthy,
            "is_available": self.is_available,
            "is_healthy": self.is_available,  # 後方互換用
            "hostname": self.hostname,
            "specs": self.specs,
            "last_heartbeat_ago": hb_ago,
            "total_requests": total,
            "failed_requests": failed,
            "success_requests": success,
            "success_rate": success_rate,
            "last_latency_ms": round(self.last_latency_ms, 1),
            "machine_name": self.machine_name,
            "engine_name": self.engine_name,
            "short_label": self.short_label,
        }


class NodeLease:
    """ノード貸出スコープ管理（非同期コンテキストマネージャー）"""

    def __init__(self, pool: 'TTSNodePool', node: TTSNode, text_len: int = 0):
        self.pool = pool
        self.node = node
        self.text_len = text_len
        self.start_time = 0.0

    async def __aenter__(self) -> TTSNode:
        self.start_time = time.time()
        return self.node

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        latency_ms = (time.time() - self.start_time) * 1000.0
        try:
            if exc_type is None:
                self.node.mark_success(latency_ms)
                if self.text_len > 0:
                    latency_profiler.record_synth(self.node, self.text_len, latency_ms)
            else:
                if isinstance(exc_val, (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, TTSConnectionError, TTSError, OSError)):
                    self.node.mark_failed(
                        cooldown_seconds=25.0,
                        reason=str(exc_val) or type(exc_val).__name__,
                        details=str(exc_val)
                    )
        finally:
            self.node.active_count = max(0, self.node.active_count - 1)
            self.node.semaphore.release()


class TTSNodePool:
    """同一エンジン種別の複数ノードを統括するプールマネージャー"""

    def __init__(self, engine_name: str, node_configs: List[TTSNodeConfig]):
        self.engine_name = engine_name
        self.nodes: List[TTSNode] = [TTSNode(c) for c in node_configs if c.enabled]
        self.nodes.sort(key=lambda n: (n.priority, n.id))
        for n in self.nodes:
            try:
                latency_profiler.get_or_create(n)
            except Exception:
                pass

    def get_nodes(self) -> List[TTSNode]:
        """全有効ノード一覧を取得"""
        return [n for n in self.nodes if n.enabled]

    def register_or_update(self, config: TTSNodeConfig, specs: Optional[Dict[str, Any]] = None, hostname: str = "") -> TTSNode:
        """Workerノードを動的に登録または更新"""
        existing = next((n for n in self.nodes if n.id == config.id), None)
        if existing:
            existing.url = config.url.rstrip("/")
            existing.priority = config.priority
            existing.enabled = config.enabled
            if specs:
                existing.specs = dict(specs)
                if "engine_running" in specs:
                    existing.engine_running = bool(specs["engine_running"])
            if hostname:
                existing.hostname = hostname
            existing.update_heartbeat()
            # 同時実行数の変更があればセマフォ更新
            if existing.max_concurrent != config.max_concurrent:
                existing.max_concurrent = max(1, config.max_concurrent)
                existing.semaphore = asyncio.Semaphore(existing.max_concurrent)
            logger.info(f"🔄 [NodePool] Node '{config.id}' updated in pool '{self.engine_name}'.")
            target = existing
        else:
            new_node = TTSNode(config, specs=dict(specs) if specs else None, hostname=hostname)
            self.nodes.append(new_node)
            logger.info(f"✨ [NodePool] New node '{config.id}' ({config.url}) registered into pool '{self.engine_name}'.")
            target = new_node

        self.nodes.sort(key=lambda n: (n.priority, n.id))
        try:
            latency_profiler.get_or_create(target)
        except Exception:
            pass
        return target

    def add_node(self, node: TTSNode) -> None:
        """ノードインスタンス（WSWorkerNode等）を直接プールに追加または既存置換"""
        existing = next((n for n in self.nodes if n.id == node.id), None)
        if existing:
            self.nodes.remove(existing)
            logger.info(f"🔄 [NodePool] Replaced existing node '{node.id}' in pool '{self.engine_name}'.")
        self.nodes.append(node)
        self.nodes.sort(key=lambda n: (n.priority, n.id))
        try:
            latency_profiler.get_or_create(node)
        except Exception:
            pass
        logger.info(f"✨ [NodePool] Node '{node.id}' ({node.summary_label}) added to pool '{self.engine_name}'.")

    def remove_node(self, node_id: str) -> Optional[TTSNode]:
        """指定IDのノードをプールから安全に除外"""
        target = next((n for n in self.nodes if n.id == node_id), None)
        if target:
            self.nodes.remove(target)
            logger.info(f"🔌 [NodePool] Node '{node_id}' removed from pool '{self.engine_name}'.")
            return target
        return None

    def update_heartbeat(self, node_id: str) -> bool:
        """指定ノードのハートビートを更新"""
        for n in self.nodes:
            if n.id == node_id:
                n.update_heartbeat()
                return True
        return False

    def check_heartbeat_timeout(self, timeout_seconds: float = 90.0):
        """ハートビートが途絶えた外部ノードを自動隔離 (ローカル自機は除外)"""
        now = time.time()
        for n in self.nodes:
            if n.id != "local_master" and n.enabled and n.is_healthy:
                elapsed = now - n.last_heartbeat
                if elapsed > timeout_seconds:
                    n.is_healthy = False
                    n.unhealthy_until = now + 15.0
                    logger.warning(f"⚠️ [NodePool] Node '{n.id}' heartbeat timed out ({elapsed:.0f}s > {timeout_seconds}s). Marked offline.")
                    try:
                        from src.utils.alert_service import alert_service, CATEGORY_WORKER, SEVERITY_WARNING
                        alert_service.notify(
                            title=f"Workerハートビート途絶 (オフライン隔離)",
                            message=f"ワーカーノード `{n.machine_name}` からの定期ハートビートが途絶えました ({elapsed:.0f}秒未達)。\n安全のため新規リクエスト割り当てから自動除外しました。",
                            category=CATEGORY_WORKER,
                            severity=SEVERITY_WARNING,
                            node_name=n.machine_name,
                            engine_name=n.engine_name
                        )
                    except Exception as e:
                        logger.debug(f"Alert notify error in check_heartbeat_timeout: {e}")

    def has_active_external_gpu_node(self) -> bool:
        """健全で利用可能な外部 GPU ノードが存在するか判定"""
        return any(n.is_available and n.is_external and n.has_gpu for n in self.nodes)

    def get_primary_url(self) -> str:
        """最も優先度が高く利用可能なノードのURLを取得"""
        for node in self.nodes:
            if node.is_available:
                return node.url
        return self.nodes[0].url if self.nodes else ""

    async def acquire_node(
        self,
        timeout: float = 6.0,
        text_len: int = 0,
        is_longest_chunk: bool = False,
        model_name: Optional[str] = None,
        guild_id: Optional[Any] = None
    ) -> Optional[NodeLease]:
        """
        エンジン毎の動的 4段階 Tier ルーティング (WebUIでエンジン毎に優先順位を設定可能)
        - デフォルト: GPU+RAMDisk -> CPU+RAMDisk -> GPU+SSD -> CPU+SSD
        - 全ノードがビジーの場合は空くまで最大 timeout 秒待機
        """
        now_ts = time.time()
        eng_key = self.engine_name.lower()
        current_order = get_tier_routing_order(eng_key)

        # 1. オンデマンド遠隔起動のトリガー判定
        # 現在設定されている Tier 優先順位の上位 Tier に属するノードが停止中の場合、起動をトリガー
        candidates_to_start = []
        for n in self.nodes:
            if n.enabled and not n.is_available and not n.draining:
                candidates_to_start.append(n)

        if candidates_to_start:
            def get_node_tier_rank(node: TTSNode) -> int:
                in_ram = node.has_model_in_ram(model_name) if model_name else bool(node.specs.get("ram_models") or node.specs.get("is_ramdisk"))
                tier_key = ("gpu" if node.has_gpu else "cpu") + "_" + ("ram" if in_ram else "ssd")
                try:
                    return current_order.index(tier_key)
                except ValueError:
                    return 99

            candidates_to_start.sort(key=lambda n: (get_node_tier_rank(n), n.priority))
            # 最上位の候補からオンデマンド遠隔起動
            for n in candidates_to_start[:2]:
                if now_ts - getattr(n, "last_start_attempt", 0.0) > 10.0:
                    n.last_start_attempt = now_ts
                    asyncio.create_task(n.try_remote_start())

        start = time.time()
        while time.time() - start < timeout:
            # ギルド専用ノード (exclusive_guild_id) および保有モデルのフィルタリング
            filtered_nodes = []
            target_gid_str = str(guild_id).strip() if guild_id is not None else None
            for n in self.nodes:
                if not n.is_available:
                    continue
                # モデル保有判定（ボランティア機等の未保有モデルへのリクエスト送出を完全防止）
                if not n.supports_model(self.engine_name, model_name):
                    continue
                ex_gid = getattr(n, "exclusive_guild_id", None)
                if ex_gid:
                    # 特定ギルド専用ノード: リクエストのギルドIDと一致する場合のみ許可
                    if target_gid_str and target_gid_str == str(ex_gid):
                        filtered_nodes.append(n)
                else:
                    # 全体共有ノード: 全リクエストに提供可能
                    filtered_nodes.append(n)

            available = filtered_nodes
            if not available:
                await asyncio.sleep(0.1)
                continue

            # 4段階 Tier バケット分類
            # ノードが対象モデルを RAMDisk に保持しているか（model_name 未指定時はエンジンの RAM 常駐フラグ判定）
            def is_node_in_ram(node: TTSNode) -> bool:
                if model_name:
                    return node.has_model_in_ram(model_name)
                return bool(node.specs.get("ram_models") or node.specs.get("is_ramdisk") or node.specs.get("in_ramdisk"))

            buckets = {
                "gpu_ram": [n for n in available if n.has_gpu and is_node_in_ram(n)],
                "cpu_ram": [n for n in available if not n.has_gpu and is_node_in_ram(n)],
                "gpu_ssd": [n for n in available if n.has_gpu and not is_node_in_ram(n)],
                "cpu_ssd": [n for n in available if not n.has_gpu and not is_node_in_ram(n)],
            }

            # バケット内ソート:
            # GPUノード: 長文 (text_len >= 18 または is_longest_chunk) の場合は最強GPU (gpu_power_score 最大) を最優先
            # 短文の場合はサブGPU等を活かして最適化
            if is_longest_chunk or text_len >= 18:
                gpu_sort_key = lambda n: (n.active_count, -latency_profiler.get_or_create(n).gpu_power_score, n.priority, n.last_latency_ms)
            else:
                gpu_sort_key = lambda n: (n.active_count, latency_profiler.get_or_create(n).gpu_power_score, n.priority, n.last_latency_ms)

            cpu_sort_key = lambda n: (n.active_count, n.priority, n.last_latency_ms)

            buckets["gpu_ram"].sort(key=gpu_sort_key)
            buckets["gpu_ssd"].sort(key=gpu_sort_key)
            buckets["cpu_ram"].sort(key=cpu_sort_key)
            buckets["cpu_ssd"].sort(key=cpu_sort_key)

            # 現在のエンジン設定に従ってノード順序を組み立て
            ordered_nodes = []
            for tier_key in current_order:
                ordered_nodes.extend(buckets.get(tier_key, []))

            tier_names_log = " | ".join(f"{k}={[n.machine_name for n in buckets[k]]}" for k in current_order)
            logger.debug(
                f"🎯 [{self.engine_name} Dynamic Routing] model={model_name or 'any'} | {tier_names_log} | "
                f"Candidate => {ordered_nodes[0].machine_name if ordered_nodes else 'None'}"
            )

            # 1. 即座に空いているノードを最優先で確保
            for node in ordered_nodes:
                if not node.semaphore.locked():
                    try:
                        await asyncio.wait_for(node.semaphore.acquire(), timeout=0.01)
                        node.active_count += 1
                        node.total_requests += 1
                        return NodeLease(self, node, text_len=text_len)
                    except asyncio.TimeoutError:
                        continue

            # 2. 全ノードがビジーの場合:
            # タイムアウトの残り時間に余裕がある場合 (残り > 0.4s) は、最優先Tierノードのセマフォ解放を待つ
            elapsed = time.time() - start
            remaining = timeout - elapsed
            if ordered_nodes and remaining > 0.4:
                best_cand = ordered_nodes[0]
                wait_time = min(0.2, max(0.01, remaining - 0.3))
                try:
                    await asyncio.wait_for(best_cand.semaphore.acquire(), timeout=wait_time)
                    best_cand.active_count += 1
                    best_cand.total_requests += 1
                    return NodeLease(self, best_cand, text_len=text_len)
                except asyncio.TimeoutError:
                    pass
            elif ordered_nodes:
                best_cand = ordered_nodes[0]
                wait_time = min(0.1, max(0.01, timeout - (time.time() - start)))
                try:
                    await asyncio.wait_for(best_cand.semaphore.acquire(), timeout=wait_time)
                    best_cand.active_count += 1
                    best_cand.total_requests += 1
                    return NodeLease(self, best_cand, text_len=text_len)
                except asyncio.TimeoutError:
                    pass

            await asyncio.sleep(0.03)

        logger.warning(f"⚠️ [NodePool] All nodes for '{self.engine_name}' are busy or unavailable (timeout={timeout}s).")
        return None

    def get_status(self) -> List[Dict[str, Any]]:
        """全ノードの健全性と統計情報を取得"""
        return [n.to_dict() for n in self.nodes]

    async def close(self) -> None:
        """プール内の全ノードのHTTPセッション・接続リソースを安全に解放"""
        for node in self.nodes:
            await node.close()
