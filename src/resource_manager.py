"""
スマートなローカルリソース管理モジュール (LocalEngineResourceManager)
- Docker ソケット (/var/run/docker.sock) 経由でローカルエンジンコンテナの起動・停止を直接制御
- VC入室時の先行プリウォーム (非同期先行起動)
- 早撃ち待機キューイング (最大5秒)
- アイドル停止 (VC無人またはエンジン未使用が5分継続時にコンテナ停止・メモリOS返却)
"""

import os
import time
import datetime
import asyncio
import logging
from typing import Dict, Any, List, Optional, Set, Tuple
import aiohttp

from src.utils.async_task import safe_create_task

logger = logging.getLogger(__name__)

DOCKER_SOCKET_PATH = "/var/run/docker.sock"

# 現在のホストIP解決 (未指定時は 127.0.0.1)
_CURRENT_NODE = os.getenv("TTSBOT_NODE", "master").lower()
LOCAL_HOST_IP = os.getenv("LOCAL_HOST_IP", "127.0.0.1")

# 対象ローカルエンジン定義
LOCAL_ENGINES: Dict[str, Dict[str, Any]] = {
    "voicevox": {
        "container_name": "ttsworker-voicevox",
        "fallback_container": "ttsbot-voicevox",
        "health_url": f"http://{LOCAL_HOST_IP}:50021/version",
        "speakers_url": f"http://{LOCAL_HOST_IP}:50021/speakers",
    },
    "sharevox": {
        "container_name": "ttsworker-sharevox",
        "fallback_container": "ttsbot-sharevox",
        "health_url": f"http://{LOCAL_HOST_IP}:50025/version",
        "speakers_url": f"http://{LOCAL_HOST_IP}:50025/speakers",
    },
    "coeiroink": {
        "container_name": "ttsworker-coeiroink",
        "fallback_container": "ttsbot-coeiroink",
        "health_url": f"http://{LOCAL_HOST_IP}:50031/docs",
        "speakers_url": f"http://{LOCAL_HOST_IP}:50031/v1/speakers",
    },
}

# リソース管理パラメータ
VC_VACANCY_TIMEOUT_SECONDS = 900  # 15分 (VCから人間全員退出後15分でエンジン自動停止・メモリOS返却)
MEMORY_CHECK_INTERVAL_SECONDS = 3600  # 1時間 (メモリ80%ガードの定期巡回間隔)
MEMORY_HOST_THRESHOLD_PERCENT = 80.0  # ホスト実質メモリ使用率 80%
MEMORY_TTS_ANOMALY_THRESHOLD_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB (TTS群の異常肥大判定)

# 並列制御パラメータ
CPU_PARALLEL_THRESHOLD = 70.0  # ホストCPU < 70% なら最大2並列
CPU_FALLBACK_THRESHOLD = 85.0  # ホストCPU > 85% なら高負荷回避のためクラウド即時フォールバック
MAX_CONCURRENT_NORMAL = 2      # 通常時最大並列数
MAX_CONCURRENT_HIGH_LOAD = 1   # 高負荷時（70%〜85%）直列化並列数


class HighLoadFallbackException(Exception):
    """ホストCPU高負荷時のクラウドフォールバック例外"""
    pass


class SynthesisTimeoutException(Exception):
    """合成スロット待機タイムアウト例外"""
    pass


class _AdaptiveSynthesisContext:
    """CPU負荷適応型のスマート非同期合成スロット"""

    def __init__(self, rm: 'LocalEngineResourceManager', provider: str, timeout: float = 8.0):
        self.rm = rm
        self.provider = provider
        self.timeout = timeout
        self.acquired = False

    async def __aenter__(self):
        start_time = time.time()
        async with self.rm._synthesis_condition:
            while True:
                # 1. ホストCPU高負荷ガード (85%超過時は待たずに即座にクラウドへ)
                current_cpu = self.rm._latest_host_cpu
                if current_cpu >= CPU_FALLBACK_THRESHOLD:
                    logger.warning(
                        f"⚠️ [Adaptive Concurrency] Host CPU is high ({current_cpu:.1f}% >= {CPU_FALLBACK_THRESHOLD}%). "
                        f"Bypassing local engine '{self.provider}' to Cloud TTS."
                    )
                    raise HighLoadFallbackException(f"Host CPU {current_cpu:.1f}% >= {CPU_FALLBACK_THRESHOLD}%")

                # 2. 許容最大並列数（70%未満なら2、70%以上なら1）
                max_concurrent = MAX_CONCURRENT_NORMAL if current_cpu < CPU_PARALLEL_THRESHOLD else MAX_CONCURRENT_HIGH_LOAD

                # 3. スロット空き判定（全体上限以内 かつ 同一エンジン空き）
                engine_in_use = self.rm._engine_active_count.get(self.provider, 0)
                if self.rm._active_synthesis_count < max_concurrent and engine_in_use < 1:
                    self.rm._active_synthesis_count += 1
                    self.rm._engine_active_count[self.provider] = engine_in_use + 1
                    self.acquired = True
                    break

                # 4. タイムアウト判定
                elapsed = time.time() - start_time
                remaining = self.timeout - elapsed
                if remaining <= 0:
                    logger.warning(f"⚠️ [Adaptive Concurrency] Wait for synthesis slot '{self.provider}' timed out ({self.timeout}s). Fallback.")
                    raise SynthesisTimeoutException(f"Wait timed out for {self.provider}")

                try:
                    await asyncio.wait_for(self.rm._synthesis_condition.wait(), timeout=min(remaining, 1.0))
                except asyncio.TimeoutError:
                    pass

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.acquired:
            async with self.rm._synthesis_condition:
                self.rm._active_synthesis_count = max(0, self.rm._active_synthesis_count - 1)
                if self.provider in self.rm._engine_active_count:
                    self.rm._engine_active_count[self.provider] = max(0, self.rm._engine_active_count[self.provider] - 1)
                self.rm._synthesis_condition.notify_all()


class LocalEngineResourceManager:
    LOCAL_ENGINES = LOCAL_ENGINES

    def __init__(self, bot=None, config=None, db=None, bot_pool=None):
        self.bot = bot
        self.bot_pool = bot_pool
        self.config = config
        self.db = db
        self.tts_manager = None
        self.coeiroink_ram_manager = None
        self.socket_path = DOCKER_SOCKET_PATH
        self.is_docker_available = os.path.exists(self.socket_path)
        self._starting_engines: Set[str] = set()
        self._engine_last_used: Dict[str, float] = {k: time.time() for k in LOCAL_ENGINES}
        self._last_human_active_time = time.time()
        self._last_memory_check_time = 0.0
        self._last_weekly_refresh_week: Optional[str] = None
        self._pending_weekly_refresh: bool = False
        self._pending_oom_refresh: bool = False
        self._idle_task: Optional[asyncio.Task] = None
        self._metrics_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # メトリクス追跡 (30秒間隔・最大60点/30分間)
        self._metrics_history: List[Dict[str, Any]] = []
        self._max_metrics_history = 60
        self._prev_host_cpu_idle: float = 0.0
        self._prev_host_cpu_total: float = 0.0
        self._latest_host_cpu: float = 15.0
        self._container_prev_cpu: Dict[str, Tuple[int, int]] = {}

        # 動的並列制御用ステート
        self._active_synthesis_count: int = 0
        self._engine_active_count: Dict[str, int] = {k: 0 for k in LOCAL_ENGINES}
        self._synthesis_condition = asyncio.Condition()
        self._engine_start_locks: Dict[str, asyncio.Lock] = {k: asyncio.Lock() for k in LOCAL_ENGINES}
        self._prewarm_semaphore = asyncio.Semaphore(4)

        # モデルプリウォーム状態管理 (target_node_or_local, voice_id) -> timestamp
        self._warmed_models: Dict[Tuple[str, str], float] = {}
        self._warmed_models_ttl: float = 900.0  # 15分間は同一モデルの重複ウォームアップを抑止
        self._has_stopped_for_vacancy: bool = False  # 無人15分停止ブロードキャストの多重連打防止用フラグ (One-shot)

        # COEIROINK RAMDisk モデル整合性管理 (Reconcile) クールダウン ((ip, model) -> last_time)
        self._ram_reconcile_cooldown: Dict[Tuple[str, str], float] = {}

        # スタンバイ待機フラグ (Standby中はエンジン停止および外部Worker Reconcileをガード)
        self._is_standby: bool = False

        if self.is_docker_available:
            logger.info("Docker socket detected. Smart local resource management is ENABLED.")
        else:
            logger.warning(f"Docker socket not found at {self.socket_path}. Container lifecycle management is disabled.")

    def set_standby_mode(self, is_standby: bool) -> None:
        """スタンバイモードの設定。スタンバイ中はエンジン停止や外部Workerモデル操作（Reconcile）を抑制"""
        self._is_standby = is_standby
        if is_standby:
            logger.info("⏸️ [ResourceManager] スタンバイモード有効化: エンジン自動停止およびWorker Reconcileを停止ガードします。")
        else:
            logger.info("▶️ [ResourceManager] スタンバイモード解除 (Active昇格): リソース管理およびスリープ監視を有効化します。")

    @property
    def is_standby(self) -> bool:
        return self._is_standby

    def _get_all_bots(self) -> List[Any]:
        """登録されている全Botインスタンスのリストを取得 (マルチBot対応)"""
        if hasattr(self, "bot_pool") and self.bot_pool and hasattr(self.bot_pool, "get_all_bots"):
            return self.bot_pool.get_all_bots()
        if isinstance(self.bot, (list, tuple)):
            return list(self.bot)
        if self.bot:
            return [self.bot]
        return []

    def is_model_warmed(self, target: str, voice_id: str) -> bool:
        """指定モデルが最近ウォームアップ済みか判定"""
        now = time.time()
        last = self._warmed_models.get((target, voice_id))
        if last is not None and (now - last) < self._warmed_models_ttl:
            return True
        return False

    def mark_model_warmed(self, target: str, voice_id: str) -> None:
        """指定モデルをウォームアップ済みとして記録"""
        now = time.time()
        self._warmed_models[(target, voice_id)] = now
        # 古いエントリのクリーンアップ (100件超えたら)
        if len(self._warmed_models) > 100:
            self._warmed_models = {k: v for k, v in self._warmed_models.items() if (now - v) < self._warmed_models_ttl}

    def invalidate_worker_warmed_models(self, worker_ip_or_target: str) -> int:
        """指定された Worker IP/ターゲットのウォームアップ履歴を無効化 (Worker再接続・再起動時用)"""
        target_str = str(worker_ip_or_target).strip()
        if not target_str:
            return 0
        keys_to_del = [k for k in self._warmed_models.keys() if target_str in str(k[0])]
        for k in keys_to_del:
            self._warmed_models.pop(k, None)
        if keys_to_del:
            logger.info(f"🔄 [Worker Pre-warm] Cleared {len(keys_to_del)} warmed model cache entries for worker '{target_str}'.")
        return len(keys_to_del)

    def _get_docker_session(self) -> aiohttp.ClientSession:
        connector = aiohttp.UnixConnector(path=self.socket_path)
        timeout = aiohttp.ClientTimeout(total=20)
        return aiohttp.ClientSession(connector=connector, timeout=timeout)

    async def get_container_status(self, provider: str) -> Optional[str]:
        """コンテナの状態 ('running', 'exited', 'created', etc.) を取得"""
        if not self.is_docker_available or provider not in LOCAL_ENGINES:
            return None

        candidates = [LOCAL_ENGINES[provider]["container_name"]]
        if "fallback_container" in LOCAL_ENGINES[provider]:
            candidates.append(LOCAL_ENGINES[provider]["fallback_container"])

        try:
            async with self._get_docker_session() as session:
                for container_name in candidates:
                    url = f"http://localhost/containers/{container_name}/json"
                    try:
                        async with session.get(url) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                state = data.get("State", {})
                                if state.get("Running", False):
                                    return "running"
                                return state.get("Status", "stopped")
                            elif resp.status == 404:
                                continue
                            else:
                                logger.warning(f"Failed to inspect container '{container_name}': HTTP {resp.status}")
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"Error querying docker container status for {provider}: {e}")
        return None

    async def is_engine_running(self, provider: str) -> bool:
        """Workerノードプールまたはコンテナが起動中かどうか"""
        if self.tts_manager:
            pool = getattr(self.tts_manager, f"{provider}_pool", None)
            if pool and any(n.is_available for n in pool.nodes):
                return True
        status = await self.get_container_status(provider)
        return status == "running"

    async def is_engine_healthy(self, provider: str) -> bool:
        """Workerノードプールまたはコンテナが起動しており、かつAPIエンドポイントが応答するか"""
        if self.tts_manager:
            pool = getattr(self.tts_manager, f"{provider}_pool", None)
            if pool and any(n.is_available for n in pool.nodes):
                return True
        if provider not in LOCAL_ENGINES:
            return True

        health_url = LOCAL_ENGINES[provider]["health_url"]
        try:
            timeout = aiohttp.ClientTimeout(total=2.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(health_url) as resp:
                    return resp.status in (200, 204)
        except Exception:
            return False

    async def start_engine(self, provider: str, wait_healthy: bool = False, timeout: float = 6.0) -> bool:
        """
        指定エンジンのコンテナを起動する。
        - Workerノードプールに利用可能ノードがあれば即座にTrue
        """
        if self.tts_manager:
            pool = getattr(self.tts_manager, f"{provider}_pool", None)
            if pool and any(n.is_available for n in pool.nodes):
                return True

        if not self.is_docker_available or provider not in LOCAL_ENGINES:
            return False

        container_name = LOCAL_ENGINES[provider]["container_name"]
        self._engine_last_used[provider] = time.time()

        # 既に起動中かつヘルスチェックOKなら即時完了 (全体ロック不要)
        if await self.is_engine_healthy(provider):
            return True

        # エンジン固有の起動ロックを取得して直列化 (他エンジンの起動や別操作をブロックしない)
        lock = self._engine_start_locks.get(provider)
        if not lock:
            lock = asyncio.Lock()
            self._engine_start_locks[provider] = lock

        async with lock:
            # ロック取得後に再チェック (直前に別タスクが起動完了させた可能性があるため)
            if await self.is_engine_healthy(provider):
                return True

            if provider in self._starting_engines:
                logger.debug(f"Engine '{provider}' is already being started.")
            else:
                self._starting_engines.add(provider)
                logger.info(f"⚡ [Pre-warm] Starting local engine container '{container_name}' for '{provider}'...")

                try:
                    url = f"http://localhost/containers/{container_name}/start"
                    async with self._get_docker_session() as session:
                        async with session.post(url) as resp:
                            # 204 No Content: 起動成功、304 Not Modified: 既に起動中
                            if resp.status in (204, 304):
                                logger.info(f"Container '{container_name}' started successfully.")
                            else:
                                err = await resp.text()
                                logger.warning(f"Docker start returned HTTP {resp.status}: {err}")
                except Exception as e:
                    error_msg = str(e) if str(e) else type(e).__name__
                    logger.error(f"Failed to start container '{container_name}': {error_msg}")
                    self._starting_engines.discard(provider)
                    return False

                # 起動リクエスト成功時、バックグラウンド監視タスクをスケジュール
                safe_create_task(
                    self._poll_until_ready(provider, timeout=20.0),
                    name=f"poll_ready_{provider}"
                )

        if not wait_healthy:
            return True

        # 同期的にヘルスチェックを待機
        return await self.wait_until_ready(provider, timeout=timeout)

    async def _poll_until_ready(self, provider: str, timeout: float = 15.0) -> bool:
        """バックグラウンドでヘルスチェックを行い、成功したら starting セットから除外"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if await self.is_engine_healthy(provider):
                logger.info(f"✨ Local engine '{provider}' is fully healthy and ready! (took {time.time() - start_time:.2f}s)")
                self._starting_engines.discard(provider)
                return True
            await asyncio.sleep(0.4)

        logger.warning(f"Engine '{provider}' health-check timed out after {timeout}s.")
        self._starting_engines.discard(provider)
        return False

    async def wait_until_ready(self, provider: str, timeout: float = 5.0) -> bool:
        """
        早撃ち対策: 音声合成直前に、対象エンジンが利用可能になるまで最大 timeout 秒待機。
        """
        if provider not in LOCAL_ENGINES:
            return True

        start_time = time.time()
        while time.time() - start_time < timeout:
            if await self.is_engine_healthy(provider):
                self._starting_engines.discard(provider)
                self._engine_last_used[provider] = time.time()
                return True
            await asyncio.sleep(0.3)

        logger.warning(f"Wait for engine '{provider}' exceeded {timeout}s limit. Will fallback.")
        return False

    async def stop_engine(self, provider: str) -> bool:
        """指定エンジンのコンテナを安全に停止し、メモリをOSに返却"""
        if not self.is_docker_available or provider not in LOCAL_ENGINES:
            return False

        container_name = LOCAL_ENGINES[provider]["container_name"]
        logger.info(f"🛑 [Idle Shutdown] Stopping local engine container '{container_name}' to free system memory...")

        try:
            url = f"http://localhost/containers/{container_name}/stop?t=5"
            async with self._get_docker_session() as session:
                async with session.post(url) as resp:
                    if resp.status in (204, 304):
                        logger.info(f"Container '{container_name}' stopped. Memory successfully returned to host OS.")
                        self._starting_engines.discard(provider)
                        return True
                    else:
                        err = await resp.text()
                        logger.warning(f"Docker stop returned HTTP {resp.status}: {err}")
                        return False
        except Exception as e:
            logger.error(f"Failed to stop container '{container_name}': {e}")
            return False

    async def restart_engine(self, provider: str) -> bool:
        """指定エンジンのコンテナを安全に再起動"""
        if not self.is_docker_available or provider not in LOCAL_ENGINES:
            return False

        container_name = LOCAL_ENGINES[provider]["container_name"]
        logger.info(f"🔄 [Restart Engine] Restarting local engine container '{container_name}'...")

        try:
            url = f"http://localhost/containers/{container_name}/restart?t=5"
            async with self._get_docker_session() as session:
                async with session.post(url) as resp:
                    if resp.status in (204, 304):
                        logger.info(f"Container '{container_name}' restarted successfully.")
                        self._starting_engines.discard(provider)
                        self._engine_last_used[provider] = time.time()
                        return True
                    else:
                        err = await resp.text()
                        logger.warning(f"Docker restart returned HTTP {resp.status}: {err}")
                        return False
        except Exception as e:
            logger.error(f"Failed to restart container '{container_name}': {e}")
            return False

    def is_any_human_in_vc(self) -> bool:
        """プール内の全Botが接続しているいずれかのVCに人間メンバー（not bot）が存在するか"""
        bots = self._get_all_bots()
        if not bots:
            return False
        for b in bots:
            for guild in b.guilds:
                vc = guild.voice_client
                if vc and vc.is_connected() and vc.channel:
                    for m in vc.channel.members:
                        if not m.bot:
                            return True
        return False

    async def stop_all_active_engines(self, reason: str = "vacancy") -> None:
        """起動中の全ローカルエンジンおよび外部ワーカーエンジンを安全に停止 (課題A-2)"""
        if self._is_standby:
            logger.info(f"⏸️ [Standby Guard] スタンバイ待機中のため全エンジン停止処理 ({reason}) をスキップします。")
            return

        # 1. 自機ローカルエンジンの停止
        for prov in LOCAL_ENGINES:
            if await self.is_engine_running(prov):
                logger.info(f"🛑 [{reason}] Stopping running engine '{prov}' to free memory...")
                await self.stop_engine(prov)

        # 2. 外部ワーカーへの一斉停止ブロードキャスト
        if self.tts_manager:
            try:
                await self._broadcast_worker_stop(reason=reason)
            except Exception as e:
                logger.debug(f"Failed to broadcast stop to external workers: {e}")

    async def _broadcast_worker_stop(self, reason: str = "vacancy") -> None:
        """全外部ワーカーノードへ一斉エンジン停止指示を送信"""
        if not self.tts_manager:
            return
        external_ips = set()
        for pool in (self.tts_manager.voicevox_pool, self.tts_manager.sharevox_pool, self.tts_manager.coeiroink_pool):
            if pool:
                for n in pool.nodes:
                    if n.is_external and n.url:
                        try:
                            from urllib.parse import urlparse
                            p = urlparse(n.url)
                            if p.hostname and p.hostname not in ("localhost", "127.0.0.1", LOCAL_HOST_IP):
                                external_ips.add(p.hostname)
                        except Exception:
                            pass

        if not external_ips:
            return

        logger.info(f"🛑 [{reason}] Broadcasting stop all engines to external workers: {external_ips}")
        async def _stop_worker(ip: str):
            try:
                url = f"http://{ip}:50030/engine/stop"
                timeout = aiohttp.ClientTimeout(total=4.0)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json={"engine": "all"}) as resp:
                        if resp.status == 200:
                            logger.info(f"✨ [Worker Stop] Successfully stopped engines on worker ({ip})")
            except Exception as e:
                logger.debug(f"Failed to stop engines on worker ({ip}): {e}")

        await asyncio.gather(*[_stop_worker(ip) for ip in external_ips], return_exceptions=True)

    async def restart_all_active_engines(self, reason: str = "refresh") -> None:
        """起動中の全ローカルエンジンを安全に再起動"""
        if self._is_standby:
            logger.info(f"⏸️ [Standby Guard] スタンバイ待機中のため全エンジン再起動処理 ({reason}) をスキップします。")
            return

        for prov in LOCAL_ENGINES:
            if await self.is_engine_running(prov):
                logger.info(f"🔄 [{reason}] Restarting running engine '{prov}'...")
                await self.restart_engine(prov)

    def get_system_memory_info(self) -> Dict[str, float]:
        """
        /proc/meminfo からホスト全体のメモリ使用状況を取得
        Returns: total_gb, available_gb, used_gb, used_percent
        """
        total = 0
        available = 0
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val_str = parts[1].strip().split()[0]
                        if key == "MemTotal":
                            total = int(val_str) * 1024
                        elif key == "MemAvailable":
                            available = int(val_str) * 1024
            if total > 0:
                used = max(0, total - available)
                used_percent = (used / total) * 100.0
                return {
                    "total_bytes": total,
                    "available_bytes": available,
                    "used_bytes": used,
                    "total_gb": total / (1024 ** 3),
                    "available_gb": available / (1024 ** 3),
                    "used_gb": used / (1024 ** 3),
                    "used_mb": used / (1024 ** 2),
                    "used_percent": used_percent,
                }
        except Exception as e:
            logger.debug(f"Failed to read /proc/meminfo: {e}")
        return {"total_gb": 0.0, "available_gb": 0.0, "used_gb": 0.0, "used_percent": 0.0}

    async def get_container_memory_bytes(self, container_name: str) -> Optional[int]:
        """コンテナの実質メモリ使用量（バイト）を取得（usage - inactive_file）"""
        if not self.is_docker_available:
            return None
        url = f"http://localhost/containers/{container_name}/stats?stream=false"
        try:
            async with self._get_docker_session() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        mem = data.get("memory_stats", {})
                        usage = mem.get("usage", 0)
                        stats = mem.get("stats", {})
                        inactive_file = stats.get("inactive_file", stats.get("total_inactive_file", 0))
                        return max(0, usage - inactive_file)
        except Exception as e:
            logger.debug(f"Failed to get memory stats for {container_name}: {e}")
        return None

    async def get_tts_containers_memory_bytes(self) -> Dict[str, int]:
        """TTS関連コンテナ群（Bot本体および各ローカルエンジン）のメモリ使用量を取得"""
        targets = ["discord-tts-bot"] + [cfg["container_name"] for cfg in LOCAL_ENGINES.values()]
        results = {}
        for cname in targets:
            mem = await self.get_container_memory_bytes(cname)
            if mem is not None:
                results[cname] = mem
        return results

    def mark_engine_used(self, provider: str) -> None:
        """エンジンが発声等で使用された時刻を更新"""
        if provider in LOCAL_ENGINES:
            self._engine_last_used[provider] = time.time()

    async def prewarm_for_users(self, guild_id: int, user_ids: List[int]) -> None:
        """
        指定されたユーザー群（VC参加者）の設定を走査し、
        停止中のローカルエンジンの先行起動および話者モデルの先行ロード（ウォームアップ）を実行。
        同一話者の重複排除、15分以内のウォームアップ済みスキップを徹底。
        """
        if not self.is_docker_available or not self.db or not self.config:
            return

        needed_providers: Set[str] = set()
        needed_voice_configs_map: Dict[str, Any] = {}
        available_speaker_ids = list(self.config.voices.keys())

        for uid in user_ids:
            try:
                settings = await self.db.get_or_create_guild_user_settings(
                    guild_id=guild_id,
                    user_id=uid,
                    available_speaker_ids=available_speaker_ids,
                    default_pitch=self.config.default_pitch,
                    default_speed=self.config.default_speed
                )
                spk_id = settings.get("speaker_id")
                if not spk_id:
                    continue

                voice_cfg = self.config.voices.get(spk_id)
                if voice_cfg and voice_cfg.provider in LOCAL_ENGINES:
                    needed_providers.add(voice_cfg.provider)
                    needed_voice_configs_map[voice_cfg.id] = voice_cfg
            except Exception as e:
                logger.debug(f"Failed to resolve settings for prewarm (uid={uid}): {e}")

        needed_voice_configs = list(needed_voice_configs_map.values())
        if not needed_providers and not needed_voice_configs:
            return

        # 1. 必要なエンジンの先行起動（非同期・起動済みならスキップ）
        for prov in needed_providers:
            if not await self.is_engine_running(prov):
                logger.info(f"Pre-warming local engine container '{prov}' for active VC members in guild {guild_id}")
                safe_create_task(
                    self.start_engine(prov, wait_healthy=False),
                    name=f"prewarm_engine_{prov}"
                )

        # 2. 各話者モデルのローカル先行ロード（ウォームアップ済みならスキップ）
        for vcfg in needed_voice_configs:
            vid = getattr(vcfg, 'id', 'unknown')

            # ★ COEIROINK モデルの場合: RAMDiskへの動的昇格 (非同期バックグラウンド)
            if getattr(vcfg, 'provider', None) == 'coeiroink' and self.coeiroink_ram_manager:
                safe_create_task(
                    self.coeiroink_ram_manager.promote_model(getattr(vcfg, 'voice_name', '')),
                    name=f"ram_promote_{vid}"
                )

            if not self.is_model_warmed("local", vid):
                safe_create_task(
                    self.warmup_speaker_model(vcfg),
                    name=f"warmup_model_{vid}"
                )
            else:
                logger.debug(f"[Pre-warm] Model '{vid}' already warmed on local within TTL. Skipping.")

        # 3. 外部GPUワーカーへの先行プリウォーム
        if self.tts_manager and needed_voice_configs:
            safe_create_task(
                self._broadcast_worker_prewarm(needed_providers, needed_voice_configs),
                name=f"broadcast_prewarm_guild_{guild_id}"
            )

    async def _broadcast_worker_prewarm(self, needed_providers: Set[str], needed_voice_configs: List[Any]) -> None:
        """外部GPUワーカーに対してもコンテナ先行起動および話者モデル事前ロードを並行実行 (セマフォ制御付き)"""
        if not self.tts_manager:
            return

        worker_targets: List[Tuple[str, str, Any]] = []
        for prov in needed_providers:
            pool = getattr(self.tts_manager, f"{prov}_pool", None)
            if pool:
                for n in pool.nodes:
                    if n.is_external and n.url:
                        try:
                            from urllib.parse import urlparse
                            p = urlparse(n.url)
                            if p.hostname and p.hostname not in ("localhost", "127.0.0.1"):
                                worker_targets.append((p.hostname, prov, n))
                        except Exception:
                            pass

        if not worker_targets:
            return

        logger.info(f"✨ [Pre-warm] Broadcasting prewarm to external workers: {[(ip, prov) for ip, prov, _ in worker_targets]}")

        # COEIROINK モデルの RAMDisk 昇格はファイルコピー（数GB）を伴い時間がかかるため、
        # タイムアウト90秒の broadcast_ram_promote を非同期バックグラウンドで並行実行する
        if "coeiroink" in needed_providers:
            seen_ci_models = set()
            for vcfg in needed_voice_configs:
                if getattr(vcfg, "provider", "") == "coeiroink":
                    model_id = getattr(vcfg, "voice_name", "") or getattr(vcfg, "id", "")
                    if model_id and model_id not in seen_ci_models:
                        seen_ci_models.add(model_id)
                        safe_create_task(
                            self.broadcast_ram_promote(model_id, pinned=False),
                            name=f"bg_ram_promote_{model_id}"
                        )

        async def _prewarm_one(ip: str, prov: str, node: Any):
            sem = getattr(self, "_prewarm_semaphore", None)
            if sem is None:
                self._prewarm_semaphore = asyncio.Semaphore(4)
                sem = self._prewarm_semaphore
            async with sem:
                # 1. 外部ワーカーのコンテナ先行起動 (POST /engine/start)
                try:
                    url = f"http://{ip}:50030/engine/start"
                    timeout = aiohttp.ClientTimeout(total=10.0)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.post(url, json={"engine": prov}) as resp:
                            if resp.status == 200:
                                logger.info(f"✨ [Worker Pre-warm] Remote engine '{prov}' ready on worker ({ip})")
                                # ノード状態を即時アクティブに更新し、UIおよびルーティングへ即時反映
                                node.engine_running = True
                                node.is_healthy = True
                                node.specs["engine_running"] = True
                                node.specs["engine_healthy"] = True
                                node.unhealthy_until = 0.0
                except Exception as e:
                    logger.warning(f"⚠️ [Worker Pre-warm] Failed to start engine '{prov}' on worker ({ip}): {e}")

                # 2. モデル事前ロード (エンジンURLへ直接リクエスト)
                for vcfg in needed_voice_configs:
                    if getattr(vcfg, "provider", "") == prov:
                        vid = getattr(vcfg, "id", "")
                        node_key = str(node.url)
                        if self.is_model_warmed(node_key, vid):
                            logger.debug(f"[Worker Pre-warm] Model '{vid}' already warmed on {node.machine_name}. Skipping.")
                            continue
                        try:
                            await self.warmup_speaker_model_on_node(node, vcfg)
                        except Exception as e:
                            logger.debug(f"Failed to warmup speaker on worker {ip}: {e}")

        results = await asyncio.gather(*[_prewarm_one(ip, prov, node) for ip, prov, node in worker_targets], return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"⚠️ [Worker Pre-warm] Exception in worker prewarm task: {r}")

    async def _promote_single_worker(self, ip: str, model_id: str, pinned: bool = True) -> bool:
        """単一Workerに対してモデルのRAMDisk昇格リクエストを送信"""
        try:
            url = f"http://{ip}:50030/models/ram_promote"
            timeout = aiohttp.ClientTimeout(total=90.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json={"model": model_id, "pinned": pinned}) as resp:
                    if resp.status == 200:
                        logger.info(f"✨ [WorkerRAM] Worker ({ip}) でのモデル昇格成功: {model_id} (pinned={pinned})")
                        return True
                    else:
                        logger.warning(f"⚠️ [WorkerRAM] Worker ({ip}) モデル昇格応答 HTTP {resp.status}: {model_id}")
        except Exception as e:
            logger.debug(f"Failed to promote model on worker ({ip}): {e}")
        return False

    async def broadcast_ram_promote(self, model_name_or_uuid: str, pinned: bool = False) -> None:
        """接続中の全Workerに対して COEIROINK モデルの RAMDisk 昇格 (Pre-warm / ピン留め) をブロードキャスト"""
        if self._is_standby:
            logger.debug(f"⏸️ [Standby Guard] スタンバイ待機中のため Worker RAM 昇格ブロードキャスト ({model_name_or_uuid}) をスキップします。")
            return

        if not self.tts_manager or not self.tts_manager.coeiroink_pool:
            return
        nodes = self.tts_manager.coeiroink_pool.nodes
        worker_ips = set()
        for n in nodes:
            if n.is_external and n.url:
                try:
                    from urllib.parse import urlparse
                    p = urlparse(n.url)
                    if p.hostname and p.hostname not in ("localhost", "127.0.0.1"):
                        worker_ips.add(p.hostname)
                except Exception:
                    pass

        if not worker_ips:
            return

        await asyncio.gather(
            *[self._promote_single_worker(ip, model_name_or_uuid, pinned=pinned) for ip in worker_ips],
            return_exceptions=True
        )

    async def get_desired_coeiroink_models(self, active_vc_members: Optional[Dict[int, List[int]]] = None) -> Set[str]:
        """
        現在 RAMDisk に常駐しているべき COEIROINK モデル（Desired State）を算出
        1. DB の個人設定・ギルド設定で指定されているピン留めモデル (ci_*)
        2. 現在 VC に滞在中の通話者が使用しているモデル
        """
        desired: Set[str] = set()
        if not self.db or not self.config:
            return desired

        # 1. DB ピン留めモデル
        try:
            async with self.db.get_connection() as conn:
                async with conn.execute(
                    "SELECT DISTINCT speaker_id FROM ("
                    "  SELECT speaker_id FROM user_settings WHERE speaker_id LIKE 'ci_%'"
                    "  UNION"
                    "  SELECT speaker_id FROM guild_user_settings WHERE speaker_id LIKE 'ci_%'"
                    ")"
                ) as cursor:
                    rows = await cursor.fetchall()
            for (spk_id,) in rows:
                v_info = self.config.voices.get(spk_id)
                if v_info and getattr(v_info, "voice_name", None):
                    spk_uuid = v_info.voice_name.split("_")[0]
                    desired.add(spk_uuid)
                elif spk_id:
                    desired.add(spk_id)
        except Exception as e:
            logger.debug(f"[ResourceManager] Error fetching pinned COEIROINK models: {e}")

        # 2. 現在 VC にいるメンバーのモデル
        if active_vc_members:
            available_speaker_ids = list(self.config.voices.keys())
            for guild_id, member_ids in active_vc_members.items():
                for uid in member_ids:
                    try:
                        settings = await self.db.get_or_create_guild_user_settings(
                            guild_id=guild_id,
                            user_id=uid,
                            available_speaker_ids=available_speaker_ids,
                            default_pitch=self.config.default_pitch,
                            default_speed=self.config.default_speed
                        )
                        spk_id = settings.get("speaker_id")
                        if spk_id and str(spk_id).startswith("ci_"):
                            v_info = self.config.voices.get(spk_id)
                            if v_info and getattr(v_info, "voice_name", None):
                                spk_uuid = v_info.voice_name.split("_")[0]
                                desired.add(spk_uuid)
                            elif spk_id:
                                desired.add(spk_id)
                    except Exception as e:
                        logger.debug(f"[ResourceManager] Error fetching VC member setting (uid={uid}): {e}")

        return desired

    async def sync_desired_ram_models_to_worker(self, worker_ip: str) -> None:
        """新規または再接続した Worker に対して Desired Models を即時同期 (Worker登録時)"""
        if self._is_standby:
            return
        if not worker_ip or worker_ip in ("localhost", "127.0.0.1"):
            return
        desired = await self.get_desired_coeiroink_models()
        if not desired:
            return
        logger.info(f"🔄 [WorkerRAM] Worker ({worker_ip}) 登録検知: 必要な COEIROINK モデル {list(desired)} の同期を開始します")
        for model_id in desired:
            await self._promote_single_worker(worker_ip, model_id, pinned=True)

    async def sync_active_vc_models_to_worker(self, worker_ip: str) -> None:
        """
        Worker 登録・再接続時に、現在 VC 通話者が使用している VOICEVOX / SHAREVOX モデルを
        その Worker へ即座に事前ロード (GPU VRAM への先行ウォームアップ)
        """
        if self._is_standby:
            return
        if not worker_ip or worker_ip in ("localhost", "127.0.0.1") or not self.tts_manager:
            return

        bots = self._get_all_bots()
        if not bots or not self.db or not self.config:
            return

        # 1. 現在 VC にいるメンバーの収集
        active_members: List[Tuple[int, int]] = []
        for b in bots:
            for guild in b.guilds:
                vc = guild.voice_client
                if vc and vc.is_connected() and vc.channel:
                    for m in vc.channel.members:
                        if not m.bot:
                            active_members.append((guild.id, m.id))

        if not active_members:
            return

        # 2. 該当メンバーの話者設定を走査
        needed_voice_configs: List[Any] = []
        available_speaker_ids = list(self.config.voices.keys())
        seen_vids = set()

        for gid, uid in active_members:
            try:
                settings = await self.db.get_or_create_guild_user_settings(
                    guild_id=gid,
                    user_id=uid,
                    available_speaker_ids=available_speaker_ids,
                    default_pitch=self.config.default_pitch,
                    default_speed=self.config.default_speed
                )
                spk_id = settings.get("speaker_id")
                if spk_id and spk_id not in seen_vids:
                    seen_vids.add(spk_id)
                    vcfg = self.config.voices.get(spk_id)
                    if vcfg and getattr(vcfg, "provider", "") in ("voicevox", "sharevox"):
                        needed_voice_configs.append(vcfg)
            except Exception as e:
                logger.debug(f"Error checking VC member voice for worker sync: {e}")

        if not needed_voice_configs:
            return

        logger.info(
            f"✨ [Worker Pre-warm] Worker ({worker_ip}) 登録検知: 現在 VC で使用中の音声モデル "
            f"{[v.name for v in needed_voice_configs]} を先行ウォームアップします"
        )

        for vcfg in needed_voice_configs:
            prov = getattr(vcfg, "provider", "")
            pool = getattr(self.tts_manager, f"{prov}_pool", None)
            if not pool:
                continue
            for n in pool.nodes:
                if n.is_external and n.url and worker_ip in n.url:
                    safe_create_task(
                        self.warmup_speaker_model_on_node(n, vcfg),
                        name=f"sync_warmup_{worker_ip}_{vcfg.id}"
                    )

    async def reconcile_worker_ram_models(self, active_vc_members: Optional[Dict[int, List[int]]] = None) -> None:
        """
        Master主導のCOEIROINK RAMDiskモデル整合性（Reconcile）定期チェック
        各Workerの現在常駐モデルと Desired State を突合し、欠落があれば自動プロモートを発行
        """
        if self._is_standby:
            logger.debug("⏸️ [Standby Guard] スタンバイ待機中のため Worker RAM Reconcile をスキップします。")
            return
        if not self.tts_manager or not self.tts_manager.coeiroink_pool:
            return

        pool = self.tts_manager.coeiroink_pool
        ext_nodes = [n for n in pool.nodes if n.is_external and n.is_available and n.url]
        if not ext_nodes:
            return

        desired = await self.get_desired_coeiroink_models(active_vc_members)
        if not desired:
            return

        now = time.time()
        for node in ext_nodes:
            from urllib.parse import urlparse
            p = urlparse(node.url)
            ip = p.hostname
            if not ip or ip in ("localhost", "127.0.0.1"):
                continue

            missing_models = [m for m in desired if not node.has_model_in_ram(m)]
            for m in missing_models:
                cd_key = (ip, m)
                last_try = self._ram_reconcile_cooldown.get(cd_key, 0.0)
                if now - last_try < 180.0:  # クールダウン3分
                    continue
                self._ram_reconcile_cooldown[cd_key] = now
                logger.warning(
                    f"⚠️ [WorkerRAM Reconcile] Worker '{node.machine_name}' ({ip}) でモデル '{m}' の未常駐を検知。"
                    f"Master主導で自動再プロモートを実行します。"
                )
                safe_create_task(
                    self._promote_single_worker(ip, m, pinned=True),
                    name=f"reconcile_promote_{ip}_{m}"
                )

    async def warmup_speaker_model_on_node(self, node: Any, voice_cfg: Any) -> bool:
        """指定された外部ノードの音声モデルをエンジンメモリ上に事前ロード"""
        if not node or not node.url or not voice_cfg:
            return False
        provider = getattr(voice_cfg, "provider", "")
        vid = getattr(voice_cfg, "id", "")
        node_key = str(node.url)

        if self.is_model_warmed(node_key, vid):
            return True

        base_url = node.url.rstrip("/")

        try:
            timeout = aiohttp.ClientTimeout(total=15.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if provider == "voicevox":
                    style_id = getattr(voice_cfg, "style_id", 3)
                    url = f"{base_url}/initialize_speaker?speaker={style_id}"
                    async with session.post(url) as resp:
                        if resp.status in (200, 204):
                            self.mark_model_warmed(node_key, vid)
                            logger.info(f"✨ [Worker Pre-warm] VOICEVOX speaker model loaded on {node.machine_name}: {voice_cfg.name}")
                            return True
                elif provider == "sharevox":
                    style_id = getattr(voice_cfg, "style_id", 14)
                    url = f"{base_url}/initialize_speaker?speaker={style_id}"
                    async with session.post(url) as resp:
                        if resp.status in (200, 204):
                            self.mark_model_warmed(node_key, vid)
                            logger.info(f"✨ [Worker Pre-warm] SHAREVOX speaker model loaded on {node.machine_name}: {voice_cfg.name}")
                            return True
                elif provider == "coeiroink":
                    voice_name = getattr(voice_cfg, "voice_name", "")
                    parts = voice_name.split("_")
                    if len(parts) >= 2:
                        spk_uuid = parts[0]
                        style_id = int(parts[1]) if parts[1].isdigit() else 0
                        url = f"{base_url}/v1/synthesis"
                        payload = {
                            "speakerUuid": spk_uuid,
                            "styleId": style_id,
                            "text": "っ",
                            "speedScale": 1.0,
                            "pitchScale": 0.0,
                            "volumeScale": 1.0,
                            "intonationScale": 1.0,
                            "prePhonemeLength": 0.05,
                            "postPhonemeLength": 0.05,
                            "outputSamplingRate": 24000
                        }
                        async with session.post(url, json=payload) as resp:
                            if resp.status == 200:
                                self.mark_model_warmed(node_key, vid)
                                logger.info(f"✨ [Worker Pre-warm] COEIROINK model preloaded on {node.machine_name}: {voice_cfg.name}")
                                return True
        except Exception as e:
            logger.debug(f"Worker model warmup failed on {node.machine_name}: {e}")
        return False

    async def warmup_voice_by_id(self, voice_id: str) -> None:
        """指定された voice_id のモデルを先行ウォームアップ（WebUI設定変更時等）"""
        if not self.config:
            return
        vcfg = self.config.voices.get(voice_id)
        if vcfg and getattr(vcfg, "provider", "") in LOCAL_ENGINES:
            safe_create_task(
                self.warmup_speaker_model(vcfg),
                name=f"warmup_by_id_{voice_id}"
            )

    async def warmup_speaker_model(self, voice_cfg: Any) -> bool:
        """
        指定されたローカル音声モデルをエンジンメモリ上に事前ロード（ウォームアップ）する。
        - VOICEVOX: POST /initialize_speaker?speaker={style_id}
        - SHAREVOX: POST /initialize_speaker?speaker={style_id}
        - COEIROINK: POST /v1/synthesis で短文ダミー推論を実行
          ※外部GPUノードが稼働している場合は、Master CPUでのダミー推論をスキップしてCPU 100%飽和を防止
        """
        if not voice_cfg:
            return False
        provider = getattr(voice_cfg, "provider", "")
        if provider not in LOCAL_ENGINES:
            return True

        vid = getattr(voice_cfg, "id", "")
        if self.is_model_warmed("local", vid):
            return True

        # コンテナが立ち上がりヘルスチェックOKになるまで待機 (最大8秒)
        ready = await self.start_engine(provider, wait_healthy=True, timeout=8.0)
        if not ready:
            return False

        try:
            timeout = aiohttp.ClientTimeout(total=30.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if provider == "voicevox":
                    style_id = getattr(voice_cfg, "style_id", 3)
                    url = f"http://voicevox:50021/initialize_speaker?speaker={style_id}"
                    async with session.post(url) as resp:
                        if resp.status in (200, 204):
                            self.mark_model_warmed("local", vid)
                            logger.info(f"✨ [Pre-warm] VOICEVOX speaker model loaded: {voice_cfg.name} (style_id={style_id})")
                            return True
                elif provider == "sharevox":
                    style_id = getattr(voice_cfg, "style_id", 14)
                    url = f"http://sharevox:50025/initialize_speaker?speaker={style_id}"
                    async with session.post(url) as resp:
                        if resp.status in (200, 204):
                            self.mark_model_warmed("local", vid)
                            logger.info(f"✨ [Pre-warm] SHAREVOX speaker model loaded: {voice_cfg.name} (style_id={style_id})")
                            return True
                elif provider == "coeiroink":
                    # Master CPU保護: 外部GPUノードが健全に稼働している場合は、Master CPUでの重いダミー推論をスキップ
                    has_external_gpu = False
                    if self.tts_manager and hasattr(self.tts_manager, "coeiroink_pool"):
                        for n in self.tts_manager.coeiroink_pool.get_nodes():
                            if n.is_external and n.has_gpu and n.is_available:
                                has_external_gpu = True
                                break

                    if has_external_gpu:
                        self.mark_model_warmed("local", vid)
                        logger.info(f"✨ [Pre-warm] COEIROINK local container ready. Skipped local CPU dummy inference because external GPU worker is available.")
                        return True

                    from src.tts.coeiroink_tts import build_coeiroink_warmup_payload
                    voice_name = getattr(voice_cfg, "voice_name", "")
                    payload = build_coeiroink_warmup_payload(voice_name)
                    if payload:
                        url = "http://coeiroink:50031/v1/synthesis"
                        async with session.post(url, json=payload) as resp:
                            if resp.status == 200:
                                self.mark_model_warmed("local", vid)
                                logger.info(f"✨ [Pre-warm] COEIROINK model & ONNX session preloaded: {voice_cfg.name} ({payload.get('speakerUuid')})")
                                return True
        except Exception as e:
            logger.debug(f"Pre-warm speaker model error for {getattr(voice_cfg, 'name', 'unknown')}: {e}")

        return False

    def start_idle_monitor(self) -> None:
        """バックグラウンドで定期的にアイドル状態監視およびメトリクス収集タスクを開始"""
        if self._is_standby:
            logger.info("⏸️ [Standby Guard] スタンバイ待機中のためアイドル監視タスクの開始をスキップします。")
            return
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = safe_create_task(self._idle_monitor_loop(), name="idle_monitor_loop")
        self.start_metrics_collector()

    async def _idle_monitor_loop(self) -> None:
        """60秒おきにVC接続状況および各エンジンの最終使用時刻をチェック"""
        logger.info("Local engine idle monitor loop started.")
        while True:
            try:
                await asyncio.sleep(60)
                await self._check_idle_engines()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Error in idle monitor loop: {e}")

    async def _check_idle_engines(self) -> None:
        if self._is_standby:
            return
        bots = self._get_all_bots()
        if not self.is_docker_available or not bots or not self.config or not self.db:
            return

        now_ts = time.time()
        now_dt = datetime.datetime.now()

        # 1. プール内の全Botが接続している全VCと、そのVCにいる人間メンバー（not bot）を収集
        active_vc_members: Dict[int, List[int]] = {}
        for b in bots:
            for guild in b.guilds:
                vc = guild.voice_client
                if vc and vc.is_connected() and vc.channel:
                    members = [m.id for m in vc.channel.members if not m.bot]
                    if members:
                        active_vc_members[guild.id] = members

        any_human_in_vc = len(active_vc_members) > 0

        # 2. VC在室状況に基づく時刻更新
        if any_human_in_vc:
            self._last_human_active_time = now_ts
            self._has_stopped_for_vacancy = False  # 人間が在室中は停止フラグをリセット
            # VCに通話者がいる間は、各エンジンの最終使用時刻も更新して落とさない（常駐＆0.2秒即答維持）
            for prov in LOCAL_ENGINES:
                self._engine_last_used[prov] = now_ts

        # 3. Master主導のCOEIROINK RAMDiskモデル整合性（Reconcile）定期チェック
        try:
            await self.reconcile_worker_ram_models(active_vc_members)
        except Exception as e:
            logger.debug(f"[ResourceManager] Error in reconcile_worker_ram_models: {e}")

        # 4. セーフティネット1: 毎週月曜 AM 11:00 定期リフレッシュの判定
        if now_dt.weekday() == 0 and now_dt.hour == 11:
            current_week = now_dt.strftime("%Y-W%W")
            if self._last_weekly_refresh_week != current_week:
                self._last_weekly_refresh_week = current_week
                if any_human_in_vc:
                    self._pending_weekly_refresh = True
                    logger.info("🗓️ [Weekly Refresh] Monday 11:00 AM reached. Humans currently in VC; deferred until 15 minutes after VC is empty.")
                else:
                    logger.info("🗓️ [Weekly Refresh] Monday 11:00 AM reached and VC is empty. Executing engine refresh...")
                    await self.stop_all_active_engines(reason="Weekly Refresh")

        # 4. セーフティネット2: メモリ80%監視ガード（1時間ごと）
        if now_ts - self._last_memory_check_time >= MEMORY_CHECK_INTERVAL_SECONDS:
            self._last_memory_check_time = now_ts
            mem_info = self.get_system_memory_info()
            host_used_pct = mem_info.get("used_percent", 0.0)

            if host_used_pct >= MEMORY_HOST_THRESHOLD_PERCENT:
                tts_mem = await self.get_tts_containers_memory_bytes()
                tts_total_bytes = sum(tts_mem.values())
                tts_total_gb = tts_total_bytes / (1024 ** 3)
                logger.warning(
                    f"⚠️ [Memory Guard] Host memory high ({host_used_pct:.1f}% used, {mem_info['used_gb']:.1f}GB / {mem_info['total_gb']:.1f}GB). "
                    f"TTS containers consuming {tts_total_gb:.2f}GB."
                )

                if tts_total_bytes >= MEMORY_TTS_ANOMALY_THRESHOLD_BYTES:
                    if not any_human_in_vc:
                        logger.warning("⚠️ [Memory Guard] VC is empty. Executing immediate emergency engine container reset...")
                        await self.restart_all_active_engines(reason="Memory Guard Reset")
                    else:
                        self._pending_oom_refresh = True
                        logger.warning("⚠️ [Memory Guard] Humans in VC. Deferring emergency reset until VC is empty.")
                else:
                    logger.info(
                        f"ℹ️ [Memory Guard] TTS containers are using normal memory ({tts_total_gb:.2f}GB < 10GB). "
                        "High host memory is caused by external processes. Skipping TTS reset to prevent false alarms."
                    )

        # 5. 保留フラグ消化 & 完全無人化後15分スリープ処理
        if not any_human_in_vc:
            time_since_last_human = now_ts - self._last_human_active_time

            # (A) メモリ80%保留フラグがある場合: 無人になった瞬間に即座にリセット（緊急性重視）
            if self._pending_oom_refresh:
                self._pending_oom_refresh = False
                logger.warning("✨ [Memory Guard] VC has become vacant! Executing deferred emergency engine reset now.")
                await self.restart_all_active_engines(reason="Deferred Memory Guard Reset")
                return

            # (B) 完全無人になってから15分経過した場合: エンジンを自動停止（通常スリープ ＆ 月曜リフレッシュ消化）
            if time_since_last_human >= VC_VACANCY_TIMEOUT_SECONDS:
                if not self._has_stopped_for_vacancy:
                    self._has_stopped_for_vacancy = True
                    if self._pending_weekly_refresh:
                        self._pending_weekly_refresh = False
                        logger.info("✨ [Weekly Refresh] VC has been empty for 15 minutes! Executing deferred weekly refresh now.")

                    # 稼働中のエンジンがあれば停止 (15分無人化時の1回のみ実行し、外部ワーカーへの毎分連打を完全に抑止)
                    await self.stop_all_active_engines(reason="Vacancy Sleep (15m)")

    # --- スマート並列制御 & メトリクス収集 ---

    def acquire_synthesis_slot(self, provider: str, timeout: float = 8.0) -> _AdaptiveSynthesisContext:
        """
        CPU負荷適応型のスマート非同期合成スロット（コンテキストマネージャー）
        """
        return _AdaptiveSynthesisContext(self, provider, timeout)

    def get_current_cpu_percent(self) -> float:
        return self._latest_host_cpu

    def start_metrics_collector(self) -> None:
        """バックグラウンドで30秒ごとのシステム・コンテナメトリクス収集タスクを開始"""
        if self._is_standby:
            logger.info("⏸️ [Standby Guard] スタンバイ待機中のためメトリクス収集タスクの開始をスキップします。")
            return
        if self._metrics_task is None or self._metrics_task.done():
            self._metrics_task = safe_create_task(self._metrics_monitor_loop(), name="metrics_monitor_loop")

    async def _metrics_monitor_loop(self) -> None:
        """30秒おきにホストおよび各エンジンコンテナのCPU・メモリ統計をサンプリング"""
        logger.info("System metrics collector loop started (interval: 30s).")

        # 起動直後にDBから過去の履歴を復元 (再起動後のWebUIグラフ継続性担保)
        if self.db and hasattr(self.db, "get_recent_metrics"):
            try:
                past_metrics = await self.db.get_recent_metrics(limit=self._max_metrics_history)
                if past_metrics:
                    self._metrics_history = past_metrics
                    logger.info(f"Restored {len(past_metrics)} metrics history points from database.")
            except Exception as e:
                logger.debug(f"Failed to restore metrics history from DB: {e}")

        # 初回収集
        await self._collect_system_metrics()
        while True:
            try:
                await asyncio.sleep(30)
                await self._collect_system_metrics()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Error in metrics collector loop: {e}")

    def _sample_host_cpu(self) -> float:
        """/proc/stat から前回のサンプリング以降のホストCPU使用率(%)を計算"""
        try:
            with open("/proc/stat", "r") as f:
                line = f.readline()
            fields = [float(x) for x in line.strip().split()[1:]]
            idle = fields[3] + fields[4]  # idle + iowait
            total = sum(fields)
            if self._prev_host_cpu_total > 0:
                idle_delta = idle - self._prev_host_cpu_idle
                total_delta = total - self._prev_host_cpu_total
                if total_delta > 0:
                    pct = 100.0 * (1.0 - (idle_delta / total_delta))
                    self._latest_host_cpu = max(0.0, min(100.0, pct))
            self._prev_host_cpu_idle = idle
            self._prev_host_cpu_total = total
            return self._latest_host_cpu
        except Exception as e:
            logger.debug(f"Failed to sample host CPU: {e}")
            return self._latest_host_cpu

    async def _collect_system_metrics(self) -> Dict[str, Any]:
        """ホストおよび全関連コンテナのリアルタイム統計を収集してリングバッファに追加"""
        if self._is_standby:
            return {}
        now = datetime.datetime.now()
        time_str = now.strftime("%H:%M:%S")
        ts = int(now.timestamp())

        # 1. ホスト統計
        host_cpu = self._sample_host_cpu()
        host_mem = self.get_system_memory_info()

        # 2. 各コンテナ統計
        containers_stats: Dict[str, Any] = {}
        targets = [
            ("bot", ["ttsbot-app-1", "ttsbot_app_1", "ttsbot-app", "discord-tts-bot"], "Bot本体"),
            ("voicevox", ["ttsworker-voicevox", "ttsbot-voicevox"], "VOICEVOX"),
            ("sharevox", ["ttsworker-sharevox", "ttsbot-sharevox"], "SHAREVOX"),
            ("coeiroink", ["ttsworker-coeiroink", "ttsbot-coeiroink"], "COEIROINK"),
        ]

        if self.is_docker_available:
            try:
                async with self._get_docker_session() as session:
                    for key, cnames, label in targets:
                        found_stats = False
                        for cname in cnames:
                            url = f"http://localhost/containers/{cname}/stats?stream=false"
                            try:
                                async with session.get(url) as resp:
                                    if resp.status == 200:
                                        data = await resp.json()
                                        mem = data.get("memory_stats", {})
                                        usage = mem.get("usage", 0)
                                        stats = mem.get("stats", {})
                                        inactive_file = stats.get("inactive_file", stats.get("total_inactive_file", 0))
                                        real_mem_mb = max(0, usage - inactive_file) / (1024 * 1024)

                                        cpu = data.get("cpu_stats", {})
                                        precpu = data.get("precpu_stats", {})
                                        cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - precpu.get("cpu_usage", {}).get("total_usage", 0)
                                        sys_delta = cpu.get("system_cpu_usage", 0) - precpu.get("system_cpu_usage", 0)
                                        online_cpus = cpu.get("online_cpus", len(cpu.get("cpu_usage", {}).get("percpu_usage", [])) or 1)
                                        cpu_percent = (cpu_delta / sys_delta * online_cpus * 100.0) if (sys_delta > 0 and cpu_delta > 0) else 0.0

                                        containers_stats[key] = {
                                            "name": label,
                                            "container": cname,
                                            "status": "running",
                                            "cpu": round(cpu_percent, 1),
                                            "cpu_percent": round(cpu_percent, 1),
                                            "mem_mb": round(real_mem_mb, 1),
                                            "memory_mb": round(real_mem_mb, 1)
                                        }
                                        found_stats = True
                                        break
                            except Exception:
                                pass
                        if not found_stats:
                            containers_stats[key] = {
                                "name": label,
                                "container": cnames[0],
                                "status": "stopped",
                                "cpu": 0.0,
                                "cpu_percent": 0.0,
                                "mem_mb": 0.0,
                                "memory_mb": 0.0
                            }
            except Exception as e:
                logger.debug(f"Docker stats collection error: {e}")

        # Bot本体のフォールバック (Docker APIで取れなかった場合は自プロセスおよび子プロセスから実測)
        bot_stat = containers_stats.get("bot")
        if not bot_stat or bot_stat.get("status") != "running" or (bot_stat.get("mem_mb", 0) == 0 and bot_stat.get("cpu_percent", 0) == 0):
            try:
                import psutil, os
                cur_proc = psutil.Process(os.getpid())
                proc_mem_bytes = cur_proc.memory_info().rss
                for child in cur_proc.children(recursive=True):
                    try:
                        proc_mem_bytes += child.memory_info().rss
                    except Exception:
                        pass
                bot_mem_mb = round(proc_mem_bytes / (1024 * 1024), 1)
                bot_cpu = round(cur_proc.cpu_percent(interval=None), 1)
                containers_stats["bot"] = {
                    "name": "Bot本体",
                    "container": "ttsbot-app-1",
                    "status": "running",
                    "cpu": bot_cpu,
                    "cpu_percent": bot_cpu,
                    "mem_mb": bot_mem_mb,
                    "memory_mb": bot_mem_mb
                }
            except Exception as e:
                logger.debug(f"psutil fallback failed: {e}")

        # 3. スナップショット組み立て
        entry = {
            "time": time_str,
            "timestamp": ts,
            "host": {
                "cpu_percent": round(host_cpu, 1),
                "cpu": round(host_cpu, 1),
                "mem_used_gb": round(host_mem.get("used_gb", 0.0), 2),
                "mem_used_mb": round(host_mem.get("used_bytes", 0.0) / (1024 * 1024), 1),
                "memory_used_mb": round(host_mem.get("used_bytes", 0.0) / (1024 * 1024), 1),
                "mem_total_gb": round(host_mem.get("total_gb", 0.0), 2),
                "mem_percent": round(host_mem.get("used_percent", 0.0), 1),
                "memory_percent": round(host_mem.get("used_percent", 0.0), 1)
            },
            "containers": containers_stats
        }

        # フロントエンドが直下参照できるように展開
        for k, v in containers_stats.items():
            entry[k] = v

        self._metrics_history.append(entry)
        if len(self._metrics_history) > self._max_metrics_history:
            self._metrics_history.pop(0)

        # データベースに永続化 (Fire-and-forget で非同期保存)
        if self.db and hasattr(self.db, "record_metric"):
            safe_create_task(self.db.record_metric(entry), name="persist_metric")

        # 高負荷アラート判定 (CPU 85% または メモリ 80% 超過)
        self._check_high_load_alert(entry)

        return entry

    def _check_high_load_alert(self, entry: Dict[str, Any]) -> None:
        """CPU/メモリの高負荷を検知し、継続時に警告ログを出力"""
        host = entry.get("host", {})
        cpu = host.get("cpu_percent", 0.0)
        mem = host.get("mem_percent", 0.0)

        is_high_cpu = cpu >= 85.0
        is_high_mem = mem >= 80.0

        if is_high_cpu or is_high_mem:
            self._high_load_count = getattr(self, "_high_load_count", 0) + 1
            # 2回連続 (1分間以上継続) で警告通知
            if self._high_load_count >= 2:
                reasons = []
                if is_high_cpu:
                    reasons.append(f"CPU {cpu:.1f}% >= 85%")
                if is_high_mem:
                    reasons.append(f"メモリ {mem:.1f}% >= 80%")
                logger.warning(
                    f"🚨 [Resource Alert] システム高負荷状態を検知 ({', '.join(reasons)})。"
                    f" 自動的に Edge-TTS 迂回または並列抑制が有効化されます。"
                )
        else:
            self._high_load_count = 0

    def get_metrics_data(self) -> Dict[str, Any]:
        """WebUIグラフおよびシステム状況表示用の最新データを返却"""
        current_cpu = self._latest_host_cpu
        if current_cpu >= CPU_FALLBACK_THRESHOLD:
            concurrency_mode = "cloud_fallback"
            max_concurrent = 0
            mode_label = "⚠️ 高負荷迂回モード (Edge-TTS自動切替)"
        elif current_cpu >= CPU_PARALLEL_THRESHOLD:
            concurrency_mode = "serial"
            max_concurrent = MAX_CONCURRENT_HIGH_LOAD
            mode_label = "🔒 直列安全モード (1並列)"
        else:
            concurrency_mode = "parallel"
            max_concurrent = MAX_CONCURRENT_NORMAL
            mode_label = f"⚡ 並列稼働モード (最大{MAX_CONCURRENT_NORMAL}並列)"

        concurrency_info = {
            "mode": concurrency_mode,
            "mode_label": mode_label,
            "max_concurrent": max_concurrent,
            "active_concurrent": self._active_synthesis_count,
            "active_synthesis": self._active_synthesis_count,
            "engine_active": dict(self._engine_active_count),
            "host_cpu": round(current_cpu, 1),
            "host_cpu_percent": round(current_cpu, 1),
            "history_points": len(self._metrics_history)
        }

        return {
            "status": concurrency_info,
            "concurrency": concurrency_info,
            "history": self._metrics_history,
            "current": self._metrics_history[-1] if self._metrics_history else None
        }
