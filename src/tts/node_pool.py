"""
マルチノード / 分散コンピュートワーカー管理プール (TTSNodePool)
- 共用マシンへの配慮: 各ノードごとに max_concurrent (同時実行数上限) を厳格に制限
- 動的スペック適応: WorkerのGPU/CPU/RAMスペックに応じた優先度・並列数の動的調整
- 動的自動登録 & ハートビート監視: Workerからの自動接続と死活監視 (オフライン時自動切り離し)
"""

import os
import re
import json
import asyncio
import time
import logging
from urllib.parse import urlparse
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
import aiohttp
from src.config import TTSNodeConfig
from src.tts.base import TTSConnectionError, TTSError
from src.utils.latency_profiler import latency_profiler
from src.utils.alert_service import (
    alert_service,
    CATEGORY_WORKER_ENGINE,
    CATEGORY_MASTER_ENGINE,
    CATEGORY_WORKER,
    SEVERITY_ERROR,
    SEVERITY_WARNING
)
from src.utils.system_info import get_host_system_info

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


def register_dynamic_coeiroink_models(mapping: Dict[str, str]) -> int:
    """
    Workerのハートビートやローカルの metas.json スキャンから得られた
    COEIROINK キャラクターUUID <-> モデルディレクトリ名 のマッピングを動的に双方向登録する。
    """
    if not mapping or not isinstance(mapping, dict):
        return 0
    added = 0
    for k, v in mapping.items():
        k_clean = str(k).strip().lower()
        v_clean = str(v).strip().lower()
        if not k_clean or not v_clean:
            continue
        if COEIROINK_MODEL_MAPPING.get(k_clean) != v_clean:
            COEIROINK_MODEL_MAPPING[k_clean] = v_clean
            added += 1
        if COEIROINK_MODEL_MAPPING.get(v_clean) != k_clean:
            COEIROINK_MODEL_MAPPING[v_clean] = k_clean
            added += 1
    return added


def scan_local_coeiroink_models(model_dir: Optional[str] = None) -> Dict[str, str]:
    """
    ローカルの COEIROINK モデルディレクトリ配下の metas.json を走査し、
    {dir_name: speaker_uuid} の辞書を返すと同時に COEIROINK_MODEL_MAPPING を動的更新する。
    """
    target_dir = Path(model_dir or os.getenv("COEIROINK_MODEL_DIR", "/app/coeiroink_models"))
    if not target_dir.exists():
        alt_dir = Path(__file__).resolve().parent.parent.parent / "coeiroink_models"
        if alt_dir.exists():
            target_dir = alt_dir
    discovered: Dict[str, str] = {}
    if not target_dir.exists() or not target_dir.is_dir():
        return discovered
    try:
        for entry in target_dir.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            dir_name = entry.name.strip()
            meta_file = entry / "metas.json"
            speaker_uuid = ""
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    speaker_uuid = str(meta.get("speakerUuid", "")).strip().lower()
                except Exception:
                    pass
            discovered[dir_name] = speaker_uuid or dir_name.lower()
        if discovered:
            register_dynamic_coeiroink_models(discovered)
    except Exception as e:
        logger.debug(f"Failed to scan local coeiroink models: {e}")
    return discovered


# 起動時にローカルモデルディレクトリを一度スキャンしてマッピングを自動補完
scan_local_coeiroink_models()



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
        self.is_direct_reachable: bool = bool(self.specs.get("is_direct_reachable", True))
        if isinstance(self.specs.get("installed_models_map"), dict):
            register_dynamic_coeiroink_models(self.specs["installed_models_map"])
        elif isinstance(self.specs.get("installed_models"), dict):
            register_dynamic_coeiroink_models(self.specs["installed_models"])

        self.semaphore = asyncio.Semaphore(self.max_concurrent)
        self.active_count = 0
        self.pending_est_ms = 0.0  # 処理中リクエストの推定合成時間の合計 (完了予測用)
        self.is_healthy = True
        self.unhealthy_until = 0.0
        self.last_heartbeat = time.time()
        self.total_requests = 0
        self.failed_requests = 0
        self.last_latency_ms = 0.0
        self.last_start_attempt = 0.0
        self._session: Optional[aiohttp.ClientSession] = None
        try:
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

    async def try_remote_start(self, timeout: float = 35.0) -> bool:
        """ワーカーのAgentに対してエンジンのオンデマンド遠隔起動を試みる (GPU・CPU・自ホスト問わず自律復帰)"""
        if not self.url:
            return False
        self.last_start_attempt = time.time()
        try:
            p = urlparse(self.url)
            ip = p.hostname
            if not ip:
                return False
            # localhost/127.0.0.1 の場合もローカルAgentポート50030へ接続
            target_ip = "127.0.0.1" if ip in ("localhost", "127.0.0.1") else ip
            engine = self.engine_name.lower()
            agent_url = f"http://{target_ip}:50030/engine/start"
            logger.info(f"⚡ [On-Demand Start] Sending remote start for '{engine}' to worker ({target_ip}:50030) (waiting up to {timeout:.0f}s)...")
            
            # コンテナ起動＋ヘルスチェック完了（最大60秒、実測10〜15秒）を待機できる専用タイムアウトで送出
            client_to = aiohttp.ClientTimeout(total=timeout, connect=5.0)
            async with aiohttp.ClientSession(timeout=client_to) as temp_session:
                async with temp_session.post(agent_url, json={"engine": engine}) as resp:
                    if resp.status == 200:
                        logger.info(f"✨ [On-Demand Start] Successfully woke engine '{engine}' on worker ({target_ip})")
                        self.is_healthy = True
                        self.engine_running = True
                        self.specs["engine_running"] = True
                        self.specs["engine_healthy"] = True
                        self.unhealthy_until = 0.0
                        return True
                    else:
                        err_text = await resp.text()
                        logger.warning(f"⚠️ [On-Demand Start] Worker ({target_ip}) returned HTTP {resp.status} for '{engine}': {err_text}")
        except Exception as e:
            logger.warning(f"Could not remote-start engine on worker {self.id} ({self.url}): {e}")
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
    def is_agent_reachable(self) -> bool:
        """
        Worker Agent (:50030) がオンラインで遠隔起動・制御リクエストを受信可能か判定。
        - 自機マスター (not is_external) は常時 True
        - 永続ファイルから初期復元されただけの未接続状態 (initial_standby=True) や
          直近90秒以上ハートビートが途絶えている外部PC (電源OFF/Agent停止) は False
        """
        if not self.is_external:
            return True
        if self.specs.get("initial_standby"):
            return False
        return (time.time() - self.last_heartbeat) <= 90.0

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
        # Active Handshake により直接HTTP到達不可と判定されたノードは除外 (WSトンネル側を利用)
        if getattr(self, "is_direct_reachable", True) is False or self.specs.get("is_direct_reachable") is False:
            return False
        return True


    @staticmethod
    def _match_coeiroink_model_identifier(model_name: str, target_collection: Any) -> bool:
        """
        指定された model_name (UUID, uuid_styleId, ディレクトリ名等) が
        target_collection (リストまたは {dir_name: uuid} 辞書) に含まれるか照合する共通ヘルパー。
        """
        if not model_name or not target_collection:
            return False

        raw = str(model_name).strip().lower()
        # スタイルIDサフィックス (_0, _10 等) やポート等の除去
        # 単純な split('_')[0] は 'model_a' などの名前を 'model' に破壊するため正規表現で末尾の数値のみ除去
        uuid_or_key = re.sub(r'[_\:]\d+$', '', raw).strip()

        candidates = {raw, uuid_or_key}
        mapped = COEIROINK_MODEL_MAPPING.get(uuid_or_key) or COEIROINK_MODEL_MAPPING.get(raw)
        if mapped:
            candidates.add(mapped.lower())
            mapped_short = mapped.split("-")[0].lower()
            if len(mapped_short) >= 3:
                candidates.add(mapped_short)

        if "-" in uuid_or_key:
            prefix_uuid = uuid_or_key.split("-")[0]
            if len(prefix_uuid) >= 6:
                candidates.add(prefix_uuid)

        items_to_check = []
        if isinstance(target_collection, dict):
            for k, v in target_collection.items():
                if k:
                    items_to_check.append(str(k))
                if v:
                    items_to_check.append(str(v))
        elif isinstance(target_collection, (list, tuple, set)):
            items_to_check.extend(str(x) for x in target_collection if x)

        for item in items_to_check:
            item_lower = item.strip().lower()
            item_stripped = re.sub(r'[_\:]\d+$', '', item_lower).strip()
            item_mapped = COEIROINK_MODEL_MAPPING.get(item_lower, "") or COEIROINK_MODEL_MAPPING.get(item_stripped, "")
            item_mapped = item_mapped.lower() if item_mapped else ""

            for cand in candidates:
                if (
                    cand == item_lower
                    or cand == item_stripped
                    or (item_mapped and (cand == item_mapped or cand == item_mapped.split("-")[0]))
                ):
                    return True
                # UUIDなどのプレフィックスマッチ (ハイフン区切りの第1セグメント同士の一致)
                if "-" in cand and "-" in item_lower:
                    if cand.split("-")[0] == item_lower.split("-")[0]:
                        return True
        return False

    def has_model_in_ram(self, model_name: Optional[str]) -> bool:
        """指定モデル（UUIDやディレクトリ名）がノードのRAMDiskに常駐しているか判定"""
        if not model_name:
            return False
        ram_models = self.specs.get("ram_models", [])
        if not ram_models:
            return False
        return self._match_coeiroink_model_identifier(model_name, ram_models)

    def supports_model(self, engine: str, model_name: Optional[str]) -> bool:
        """指定されたモデル（話者UUID等）をこのノードが保有・サポートしているか判定"""
        if not model_name:
            return True
        eng_clean = str(engine or self.engine_name).strip().lower()
        if eng_clean != "coeiroink":
            return True

        # specs に installed_models / installed_models_map が明示されている場合は保有チェックを実施
        installed_map = self.specs.get("installed_models_map")
        installed_list = self.specs.get("installed_models")

        if installed_map is None and installed_list is None:
            # 未報告の旧バージョンWorkerまたはlocal_masterは後方互換のためTrue
            return True

        if installed_map:
            if self._match_coeiroink_model_identifier(model_name, installed_map):
                return True
        if installed_list:
            if self._match_coeiroink_model_identifier(model_name, installed_list):
                return True

        return False

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

        # スリープ中・コールドスタート起動中（接続拒否・切断）の判定
        is_sleeping_or_starting = (
            self.engine_running is False
            or self.specs.get("engine_running") is False
            or (now - getattr(self, "last_start_attempt", 0.0) < 60.0)
        )
        is_conn_error = any(kw in str(reason).lower() for kw in [
            "server disconnected", "connection refused", "connect call failed",
            "cannot connect to host", "clientconnectorerror", "connectionreseterror"
        ])

        if is_sleeping_or_starting and is_conn_error:
            logger.info(
                f"ℹ️ [NodePool] Node '{self.id}' ({self.url}) is offline or starting up from sleep ({reason}). "
                f"Suppressing error alert DM as this is normal cold-start fallback."
            )
            return

        try:
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
        if isinstance(self.specs, dict) and self.specs.get("initial_standby"):
            self.specs.pop("initial_standby", None)
        # エンジンが停止中 (False) の場合は、ハートビートを受信しても勝手に is_healthy = True に復帰させない
        is_eng_running = (self.engine_running is not False) and (self.specs.get("engine_running", True) is not False)
        if not self.is_healthy and self.unhealthy_until <= time.time():
            if is_eng_running:
                self.is_healthy = True
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

        merged_specs = dict(self.specs)
        # 自機マスターノード、または specs にホスト詳細が不足している場合の自動補完
        if not self.is_external or not merged_specs.get("os_name") or not merged_specs.get("docker_version"):
            try:
                sys_info = get_host_system_info()
                if not self.is_external:
                    if not merged_specs.get("hostname") or merged_specs.get("hostname") == "local_master":
                        merged_specs["hostname"] = sys_info.get("hostname", "PrimaryNode")
                        merged_specs["host_hostname"] = sys_info.get("hostname", "PrimaryNode")
                    if not merged_specs.get("os_name") or merged_specs.get("os_name") == "Linux":
                        merged_specs["os_name"] = sys_info.get("os_name", "Linux")
                        merged_specs["os"] = sys_info.get("os_name", "Linux")
                    if not merged_specs.get("docker_version") or merged_specs.get("docker_version") == "N/A":
                        merged_specs["docker_version"] = sys_info.get("docker_version", "N/A")
                else:
                    if not merged_specs.get("os_name") and merged_specs.get("os"):
                        merged_specs["os_name"] = merged_specs["os"]
            except Exception:
                pass

        resolved_host = merged_specs.get("host_hostname") or merged_specs.get("hostname") or self.hostname

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
            "hostname": resolved_host,
            "specs": merged_specs,
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
        # 取得時点で処理中見込みに加算 (完了予測最短ノード選択の backlog として使用)
        self.est_ms = pool.estimate_node_ms(node, text_len) if text_len > 0 else 0.0
        node.pending_est_ms += self.est_ms

    async def __aenter__(self) -> TTSNode:
        self.start_time = time.time()
        return self.node

    def _release_pending(self) -> None:
        self.node.pending_est_ms = max(0.0, self.node.pending_est_ms - self.est_ms)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        latency_ms = (time.time() - self.start_time) * 1000.0
        # 投機負け等でキャンセルされても、エンジン側は計算を続ける前提 (未実証のため保守側) で、
        # 推定完了時刻までは処理中見込みを残す
        remain_s = (self.start_time + self.est_ms / 1000.0) - time.time()
        if exc_type is asyncio.CancelledError and remain_s > 0:
            try:
                asyncio.get_running_loop().call_later(remain_s, self._release_pending)
            except RuntimeError:
                self._release_pending()
        else:
            self._release_pending()
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
                if "is_direct_reachable" in specs:
                    existing.is_direct_reachable = bool(specs["is_direct_reachable"])
                if isinstance(specs.get("installed_models_map"), dict):
                    register_dynamic_coeiroink_models(specs["installed_models_map"])
                elif isinstance(specs.get("installed_models"), dict):
                    register_dynamic_coeiroink_models(specs["installed_models"])
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

    async def wake_up_nodes(self, wait_healthy: bool = True, timeout: float = 35.0) -> bool:
        """
        スリープ中の外部Workerノード（GPU最優先）に対してオンデマンド遠隔起動を一括送出し、
        いずれかのノードがヘルスチェックOK（is_available）になるまで待機する (バッチ事前生成・コールドスタート用)。
        """
        if any(n.is_available for n in self.nodes):
            return True

        candidates = [n for n in self.nodes if n.enabled and not n.draining and n.is_agent_reachable]
        if not candidates:
            return False

        # GPUノードを最優先、次にCPUノード
        candidates.sort(key=lambda n: (0 if n.has_gpu else 1, n.priority))

        # 上位候補（GPUノードおよびCPUノード）にリモート起動を並行送信
        tasks = []
        for n in candidates[:3]:
            tasks.append(n.try_remote_start(timeout=timeout))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            logger.info(f"⚡ [NodePool] Wake-up broadcast for '{self.engine_name}' returned: {results}")

        if not wait_healthy:
            return any(n.is_available for n in self.nodes)

        # いずれかのノードが利用可能になるまでポーリング待機 (最大 timeout 秒)
        start_t = time.time()
        while time.time() - start_t < timeout:
            if any(n.is_available for n in self.nodes):
                logger.info(f"✨ [NodePool] At least one node for '{self.engine_name}' is fully ready (took {time.time() - start_t:.2f}s)")
                return True
            await asyncio.sleep(0.5)

        ready = any(n.is_available for n in self.nodes)
        if not ready:
            logger.warning(f"⚠️ [NodePool] Wake-up for '{self.engine_name}' timed out after {timeout:.1f}s.")
        return ready

    def _order_available_nodes(
        self,
        available: List[TTSNode],
        text_len: int = 0,
        is_longest_chunk: bool = False,
        model_name: Optional[str] = None
    ) -> List[TTSNode]:
        """利用可能ノード一覧を Tier 設定およびエンジン別動的チャンク長・GPUパワースコアに従って整列"""
        eng_key = self.engine_name.lower()
        current_order = get_tier_routing_order(eng_key)

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

        # エンジン・稼働ノードに応じた動的最大チャンク長 (8〜30文字) の60%を長文閾値として連動
        dyn_max = latency_profiler.get_dynamic_max_chars(self.engine_name, candidate_nodes=available)
        long_threshold = max(8, int(dyn_max * 0.6))

        if is_longest_chunk or text_len >= long_threshold:
            gpu_sort_key = lambda n: (n.active_count, -latency_profiler.get_or_create(n).gpu_power_score, n.priority, n.last_latency_ms)
        else:
            gpu_sort_key = lambda n: (n.active_count, latency_profiler.get_or_create(n).gpu_power_score, n.priority, n.last_latency_ms)

        cpu_sort_key = lambda n: (n.active_count, n.priority, n.last_latency_ms)

        buckets["gpu_ram"].sort(key=gpu_sort_key)
        buckets["gpu_ssd"].sort(key=gpu_sort_key)
        buckets["cpu_ram"].sort(key=cpu_sort_key)
        buckets["cpu_ssd"].sort(key=cpu_sort_key)

        ordered_nodes: List[TTSNode] = []
        for tier_key in current_order:
            ordered_nodes.extend(buckets.get(tier_key, []))
        return ordered_nodes

    def estimate_node_ms(self, node: TTSNode, text_len: int) -> float:
        """ノード×エンジンの実測プロファイルから単発合成時間 (ms) を推定"""
        try:
            return float(latency_profiler.get_or_create(node).estimate(max(1, int(text_len))))
        except Exception:
            return 0.0

    def predict_eta_ms(self, node: TTSNode, text_len: int) -> float:
        """今このノードに投入した場合の完了予測 (ms)。エンジンは実質直列 (CPU 実測) とみなし、処理中見込みを前に積む"""
        return node.pending_est_ms + self.estimate_node_ms(node, text_len)

    def _earliest_finish_candidates(self, available: List[TTSNode], model_name: Optional[str]) -> List[TTSNode]:
        """モデルをRAMに保持するノードがあればそれに限定 (SSDロードの見積り外れを避ける)"""
        if model_name:
            in_ram = [n for n in available if n.has_model_in_ram(model_name)]
            if in_ram:
                return in_ram
        return available

    def peek_earliest_finish(
        self,
        text_len: int,
        model_name: Optional[str] = None,
        guild_id: Optional[Any] = None
    ) -> Tuple[Optional[TTSNode], float]:
        """完了予測が最短のノードと予測時間 (ms) をロック取得なしで参照 (先頭パーツ計画・Preroll 推定用)"""
        available = self._filter_available_nodes(model_name, guild_id)
        cands = self._earliest_finish_candidates(available, model_name)
        if not cands:
            return None, 0.0
        best = min(cands, key=lambda n: (self.predict_eta_ms(n, text_len), n.priority))
        return best, self.predict_eta_ms(best, text_len)

    def _filter_available_nodes(self, model_name: Optional[str], guild_id: Optional[Any]) -> List[TTSNode]:
        target_gid_str = str(guild_id).strip() if guild_id is not None else None
        available: List[TTSNode] = []
        for n in self.nodes:
            if not n.is_available:
                continue
            if not n.supports_model(self.engine_name, model_name):
                continue
            ex_gid = getattr(n, "exclusive_guild_id", None)
            if ex_gid:
                if target_gid_str and target_gid_str == str(ex_gid):
                    available.append(n)
            else:
                available.append(n)
        return available

    def peek_best_node(
        self,
        is_longest_chunk: bool = True,
        model_name: Optional[str] = None,
        guild_id: Optional[Any] = None
    ) -> Optional[TTSNode]:
        """現在オンラインで利用可能な最優先候補ノードをロック取得なしで参照 (Adaptive Preroll 推定用)"""
        target_gid_str = str(guild_id).strip() if guild_id is not None else None
        available: List[TTSNode] = []
        for n in self.nodes:
            if not n.is_available:
                continue
            if not n.supports_model(self.engine_name, model_name):
                continue
            ex_gid = getattr(n, "exclusive_guild_id", None)
            if ex_gid:
                if target_gid_str and target_gid_str == str(ex_gid):
                    available.append(n)
            else:
                available.append(n)
        if not available:
            return None
        ordered = self._order_available_nodes(available, is_longest_chunk=is_longest_chunk, model_name=model_name)
        return ordered[0] if ordered else None

    async def acquire_node(
        self,
        timeout: float = 6.0,
        text_len: int = 0,
        is_longest_chunk: bool = False,
        model_name: Optional[str] = None,
        guild_id: Optional[Any] = None,
        prefer_earliest_finish: bool = False
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
        # 現在設定されている Tier 優先順位の上位 Tier に属し、かつ Agent が生存しているノードが停止中の場合のみ起動をトリガー
        candidates_to_start = []
        for n in self.nodes:
            if n.enabled and not n.is_available and not n.draining and n.is_agent_reachable:
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
            for n in candidates_to_start:
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

            # 先頭パーツ: 空き枠優先ではなく完了予測最短ノードへ (枠が埋まっていてもそのノードの空きを待つ)
            if prefer_earliest_finish:
                cands = self._earliest_finish_candidates(available, model_name)
                best = min(cands, key=lambda n: (self.predict_eta_ms(n, text_len), n.priority))
                try:
                    await asyncio.wait_for(best.semaphore.acquire(), timeout=0.05)
                    best.active_count += 1
                    best.total_requests += 1
                    return NodeLease(self, best, text_len=text_len)
                except asyncio.TimeoutError:
                    continue  # 50ms ごとに再評価 (他ノードの予測が下回れば乗り換え)

            ordered_nodes = self._order_available_nodes(
                available,
                text_len=text_len,
                is_longest_chunk=is_longest_chunk,
                model_name=model_name
            )
            logger.debug(
                f"🎯 [{self.engine_name} Dynamic Routing] model={model_name or 'any'} | "
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
