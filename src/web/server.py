import os
import re
import json
import time
import logging
import asyncio
import urllib.parse
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from collections import deque
from dataclasses import asdict
import aiosqlite
import base64
import secrets
from datetime import datetime
import socket
import io
import tarfile
import hashlib
import ipaddress
import uuid
import aiohttp
from aiohttp import web
import discord

from src.config import AppConfig, save_yaml_config
from src.database import Database
from src.cache_manager import CacheManager
from src.tts.manager import TTSManager
from src.tts.coeiroink_models import CoeiroinkModelManager
from src.tts.mycoeiroink import MyCoeiroinkManager
from src.version import BUILD_VERSION, BUILD_DATE, AGENT_VERSION
from src.utils.async_task import safe_create_task
from src.utils.latency_tracker import latency_tracker
from src.utils.avatar_manager import get_avatar_path, has_avatar
from src.utils.security import validate_safe_worker_ip, validate_safe_path, is_safe_identifier, safe_json_for_html_script, sanitize_filename
from src.utils.system_info import get_host_system_info
from .auth import DiscordAuth

logger = logging.getLogger(__name__)


def _version_is_newer(remote: str, local: str) -> bool:
    """"3.12.0" 形式のバージョン比較 (remote > local)"""
    try:
        r_parts = [int(x) for x in str(remote).split(".")]
        l_parts = [int(x) for x in str(local).split(".")]
        return r_parts > l_parts
    except Exception:
        return False


class WebServer:
    @property
    def bot(self):
        if getattr(self, "_bot", None):
            return self._bot
        if self.bot_pool:
            return self.bot_pool.get_main_bot()
        return None

    @bot.setter
    def bot(self, val):
        self._bot = val

    def __init__(
        self,
        bot,
        config: AppConfig,
        database: Database,
        cache_manager: CacheManager,
        tts_manager: TTSManager,
        bot_pool=None,
        disaster_service=None
    ):
        self._bot = bot
        self.config = config
        self.db = database
        self.cache_manager = cache_manager
        self.tts_manager = tts_manager
        self.bot_pool = bot_pool
        self.disaster_service = disaster_service
        self.worker_ws_connections: Dict[str, Any] = {}
        self.worker_ws_pending_requests: Dict[str, Any] = {}
        self.coeiroink_model_manager = CoeiroinkModelManager()
        self.mycoeiroink = MyCoeiroinkManager()
        self.auth = DiscordAuth(
            client_id=config.discord_client_id,
            client_secret=config.discord_client_secret,
            redirect_uri=config.discord_redirect_uri,
            session_secret=config.web_session_secret,
            bot=bot
        )
        # Discord API レートリミット対策キャッシュ ((guild_id, user_id) -> (timestamp, Optional[discord.Member]))
        self._member_cache: Dict[Tuple[int, int], Tuple[float, Optional[discord.Member]]] = {}
        # ユーザーキャッシュ (user_id -> (timestamp, Optional[discord.User]))
        self._user_cache: Dict[int, Tuple[float, Optional[discord.User]]] = {}
        # WebUI API レート制限用スライディングバッファ
        self._rate_limits: Dict[str, deque] = {}
        self._rate_limit_cleanup_counter = 0
        self._failed_password_attempts: Dict[str, List[float]] = {}
        self._cached_stats_data: Optional[Dict[str, Any]] = None
        self._cached_stats_time: float = 0.0
        # Worker ノード別時系列メトリクス履歴 (nodeKey -> deque[dict])
        self.workers_metrics_history: Dict[str, deque] = {}
        self.workers_latest_resources: Dict[str, Dict[str, Any]] = {}
        # Phase 4: ピギーバック自動更新 調停状態管理 (1台ずつドレイン・更新)
        self.TARGET_MANIFEST_VERSION = "2026.10.03.1"
        self.PIGGYBACK_UPDATE_TIMEOUT = 900.0  # 15分タイムアウト
        self.PIGGYBACK_MIN_AGENT_VERSION = "3.12.0"  # update_available に対応した最初の Agent バージョン
        self._piggyback_cooldown_until: Dict[str, float] = {}  # ホストIP -> 再許可可能になる時刻
        self._piggyback_updating_host: Optional[str] = None
        self._piggyback_updating_at: float = 0.0
        # 大容量モデルZIP (最大500MB) のアップロードを許可するため、client_max_size を 1GB に設定
        self.app = web.Application(
            client_max_size=1024 * 1024 * 1024,
            middlewares=[self._security_and_rate_limit_middleware]
        )
        self._start_time = time.time()
        self.master_instance_id = f"master_{uuid.uuid4().hex[:12]}"
        self.ha_slaves: Dict[str, Dict[str, Any]] = {}
        self.ha_revoked_nodes: set = set()
        self.ha_nodes_file = Path(self.config.db_path).parent / "ha_manager_nodes.json"
        self._load_ha_nodes()
        self.ha_force_update_time: float = 0.0
        self.ha_slave_force_update_nodes: Dict[str, float] = {}
        self._code_hash_cache = None
        self._code_hash_cache_time = 0.0
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self._setup_routes()

    def _load_ha_nodes(self):
        try:
            if self.ha_nodes_file.exists():
                data = json.loads(self.ha_nodes_file.read_text(encoding="utf-8"))
                self.ha_slaves = data.get("slaves", {})
                self.ha_revoked_nodes = set(data.get("revoked_node_ids", []))
        except Exception as e:
            logger.error(f"[HA] Failed to load ha_manager_nodes.json: {e}")

    def _save_ha_nodes(self):
        try:
            data = {
                "slaves": self.ha_slaves,
                "revoked_node_ids": list(self.ha_revoked_nodes)
            }
            self.ha_nodes_file.parent.mkdir(parents=True, exist_ok=True)
            self.ha_nodes_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error(f"[HA] Failed to save ha_manager_nodes.json: {e}")

    def _get_next_available_priority(self, exclude_node_id: str = "") -> int:
        """未割り当ての最小 Priority (1, 2, 3...) を計算"""
        used_priorities = {
            s.get("priority") for nid, s in self.ha_slaves.items()
            if nid != exclude_node_id and isinstance(s.get("priority"), int) and s.get("priority") > 0
        }
        p = 1
        while p in used_priorities:
            p += 1
        return p

    def _record_ha_slave_heartbeat(
        self,
        node_id: str,
        hostname: str,
        ip: str,
        is_sync: bool = False,
        user_agent: str = "",
        cpu_cores: int = 1,
        memory_gb: float = 0.0,
        os_name: str = "",
        requested_priority: Optional[int] = None,
        local_ip: str = "",
        docker_version: str = "",
        is_direct_verified: bool = False,
        direct_url: str = "",
        container_hostname: str = ""
    ):
        if not node_id:
            return
        now = time.time()
        if node_id not in self.ha_slaves:
            # 参加順または指定値に基づいて空き Priority を自動採番
            priority = requested_priority if (requested_priority and requested_priority > 0) else self._get_next_available_priority(node_id)
            self.ha_slaves[node_id] = {
                "node_id": node_id,
                "hostname": hostname,
                "container_hostname": container_hostname,
                "ip": ip,
                "local_ip": local_ip,
                "is_direct_verified": is_direct_verified,
                "direct_url": direct_url,
                "first_seen": now,
                "last_seen": now,
                "last_sync": now if is_sync else 0.0,
                "status": "online",
                "user_agent": user_agent,
                "priority": priority,
                "cpu_cores": cpu_cores,
                "memory_gb": memory_gb,
                "os_name": os_name,
                "docker_version": docker_version
            }
        else:
            entry = self.ha_slaves[node_id]
            entry["hostname"] = hostname or entry.get("hostname", "unknown")
            if container_hostname:
                entry["container_hostname"] = container_hostname
            entry["ip"] = ip or entry.get("ip", "unknown")
            if local_ip:
                entry["local_ip"] = local_ip
            entry["is_direct_verified"] = is_direct_verified
            if direct_url:
                entry["direct_url"] = direct_url
            entry["last_seen"] = now
            if is_sync:
                entry["last_sync"] = now
            if node_id in self.ha_revoked_nodes:
                entry["status"] = "revoked"
            else:
                entry["status"] = "online"
            if user_agent:
                entry["user_agent"] = user_agent
            # priority が未設定の場合は補正
            if "priority" not in entry or not isinstance(entry["priority"], int) or entry["priority"] <= 0:
                entry["priority"] = self._get_next_available_priority(node_id)
            if cpu_cores > 0:
                entry["cpu_cores"] = cpu_cores
            if memory_gb > 0:
                entry["memory_gb"] = memory_gb
            if os_name:
                entry["os_name"] = os_name
            if docker_version:
                entry["docker_version"] = docker_version
        self._save_ha_nodes()

    async def _get_or_fetch_member(self, guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
        """
        ギルドメンバーを取得。Botのメモリキャッシュ ➔ 内部キャッシュ ➔ Discord REST API の順で探索。
        404 (退出済み) もキャッシュして無駄な API リクエストによるレートリミットを防止する。
        """
        m = guild.get_member(user_id)
        if m:
            return m

        cache_key = (guild.id, user_id)
        now = time.time()
        if cache_key in self._member_cache:
            ts, cached_m = self._member_cache[cache_key]
            ttl = 600.0 if cached_m is not None else 1800.0
            if now - ts < ttl:
                return cached_m

        try:
            member = await guild.fetch_member(user_id)
            self._member_cache[cache_key] = (now, member)
            return member
        except Exception:
            self._member_cache[cache_key] = (now, None)
            return None

    async def _get_or_fetch_user(self, user_id: int) -> Optional[discord.User]:
        """
        Discord ユーザーを取得。内部キャッシュ ➔ Botキャッシュ ➔ fetch_user の順で探索。
        """
        u = self.bot.get_user(user_id)
        if u:
            return u

        now = time.time()
        if user_id in self._user_cache:
            ts, cached_u = self._user_cache[user_id]
            ttl = 600.0 if cached_u is not None else 1800.0
            if now - ts < ttl:
                return cached_u

        try:
            user = await self.bot.fetch_user(user_id)
            self._user_cache[user_id] = (now, user)
            return user
        except Exception:
            self._user_cache[user_id] = (now, None)
            return None

    def _setup_routes(self):
        # ページ & スクリプト配信
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/login", self.handle_login)
        self.app.router.add_get("/callback", self.handle_callback)
        self.app.router.add_get("/logout", self.handle_logout)
        self.app.router.add_get("/setup-worker.sh", self.handle_setup_worker_sh)
        self.app.router.add_get("/static/setup-worker.sh", self.handle_setup_worker_sh)
        self.app.router.add_get("/setup-worker.ps1", self.handle_setup_worker_ps1)
        self.app.router.add_get("/static/setup-worker.ps1", self.handle_setup_worker_ps1)
        self.app.router.add_get("/setup-standby.sh", self.handle_setup_standby_sh)
        self.app.router.add_get("/setup-standby.ps1", self.handle_setup_standby_ps1)
        self.app.router.add_get("/update.sh", self.handle_update_sh)
        self.app.router.add_get("/docker-compose.yml", self.handle_docker_compose_yml)
        self.app.router.add_get("/docker-compose.slave.yml", self.handle_docker_compose_slave_yml)
        self.app.router.add_get("/portainer-stack.yml", self.handle_portainer_stack_yml)
        self.app.router.add_get("/static/agent.py", self.handle_worker_agent_script)

        # ボランティアWorkerツール配信 (全ユーザー向け)
        self.app.router.add_get("/volunteer/run_volunteer.bat", self.handle_volunteer_file)
        self.app.router.add_get("/volunteer/run_volunteer_gui.ps1", self.handle_volunteer_file)
        self.app.router.add_get("/volunteer/run_volunteer.sh", self.handle_volunteer_file)
        self.app.router.add_get("/volunteer/volunteer_worker.py", self.handle_volunteer_file)
        self.app.router.add_get("/volunteer/ttsbot-volunteer-worker.zip", self.handle_volunteer_zip)

        # API (認証必要)
        self.app.router.add_get("/api/me", self.api_get_me)
        self.app.router.add_post("/api/me/voice", self.api_update_voice)
        self.app.router.add_get("/api/me/voices_by_guild", self.api_get_user_guild_voices)
        self.app.router.add_post("/api/me/sync_voice", self.api_sync_voice)
        self.app.router.add_post("/api/preview", self.api_preview_audio)
        self.app.router.add_get("/api/voices", self.api_get_voices)
        self.app.router.add_get("/api/guilds", self.api_get_guilds)
        self.app.router.add_get("/api/guilds/{guild_id}/roles", self.api_get_guild_roles)
        self.app.router.add_get("/api/guilds/{guild_id}/channels", self.api_get_guild_channels)
        self.app.router.add_get("/api/dict", self.api_get_dict)
        self.app.router.add_post("/api/dict", self.api_add_dict)
        self.app.router.add_put("/api/dict", self.api_update_dict)
        self.app.router.add_delete("/api/dict/{word}", self.api_delete_dict)
        self.app.router.add_get("/api/ng", self.api_get_ng)
        self.app.router.add_post("/api/ng", self.api_add_ng)
        self.app.router.add_delete("/api/ng/{word}", self.api_delete_ng)
        self.app.router.add_post("/api/ng/import_preset", self.api_import_ng_preset)
        self.app.router.add_post("/api/dict/import_users", self.api_import_users)
        self.app.router.add_post("/api/dict/import_batch", self.api_import_dict_batch)
        self.app.router.add_get("/api/dict/ai/suggestions", self.api_get_ai_suggestions)
        self.app.router.add_post("/api/dict/ai/suggestions/{id}/approve", self.api_approve_ai_suggestion)
        self.app.router.add_post("/api/dict/ai/suggestions/{id}/reject", self.api_reject_ai_suggestion)
        self.app.router.add_delete("/api/dict/ai/suggestions/{id}", self.api_delete_ai_suggestion)
        self.app.router.add_get("/api/config", self.api_get_config)
        self.app.router.add_post("/api/config", self.api_update_config)
        self.app.router.add_post("/api/config/reload", self.api_reload_config)
        self.app.router.add_get("/api/stats", self.api_get_stats)
        self.app.router.add_post("/api/cache/purge", self.api_purge_cache)
        self.app.router.add_post("/api/cache/scan", self.api_scan_cache)
        self.app.router.add_get("/api/cache/stats/summary", self.api_get_cache_stats_summary)
        self.app.router.add_get("/api/cache/files", self.api_list_cache_files)
        self.app.router.add_get("/api/cache/audio/{target}/{filename}", self.api_get_cache_audio)
        self.app.router.add_delete("/api/cache/file/{target}/{filename}", self.api_delete_cache_file)
        self.app.router.add_post("/api/cache/delete_batch", self.api_delete_cache_files_batch)
        self.app.router.add_get("/api/system/models/status", self.api_get_models_status)
        self.app.router.add_post("/api/system/models/update", self.api_trigger_model_update)
        self.app.router.add_get("/api/system/models/progress", self.api_get_model_progress)
        self.app.router.add_get("/api/system/metrics", self.api_get_system_metrics)
        self.app.router.add_get("/api/system/profiler/stats", self.api_get_profiler_stats)
        self.app.router.add_post("/api/system/latency/reset", self.api_reset_latency)
        self.app.router.add_get("/api/system/latency/incidents", self.api_get_latency_incidents)
        self.app.router.add_get("/api/mycoeiroink/list", self.api_get_mycoeiroink_list)
        self.app.router.add_post("/api/mycoeiroink/sync", self.api_sync_mycoeiroink)
        self.app.router.add_get("/api/mycoeiroink/character/{uuid}", self.api_get_mycoeiroink_character)
        self.app.router.add_post("/api/mycoeiroink/install_auto", self.api_install_auto_mycoeiroink)
        self.app.router.add_post("/api/mycoeiroink/upload", self.api_upload_mycoeiroink_zip)
        self.app.router.add_post("/api/mycoeiroink/upload/chunk", self.api_upload_mycoeiroink_chunk)
        self.app.router.add_get("/api/character_terms", self.api_get_character_terms)
        self.app.router.add_post("/api/disaster/test", self.api_trigger_disaster_test)
        self.app.router.add_post("/api/disaster/config", self.api_update_disaster_config)
        self.app.router.add_get("/api/logs", self.api_get_logs)
        self.app.router.add_get("/api/admin/guild_users", self.api_admin_get_guild_users)
        self.app.router.add_put("/api/admin/guild_users/{user_id}", self.api_admin_update_guild_user)
        self.app.router.add_delete("/api/admin/guild_users/{user_id}", self.api_admin_delete_guild_user)
        self.app.router.add_get("/api/avatar/{user_id}", self.api_get_avatar)

        # Worker ノード管理 API (認証不要 - LAN内Workerからの自動登録用)
        self.app.router.add_post("/api/worker/register", self.api_worker_register)
        self.app.router.add_post("/api/worker/heartbeat", self.api_worker_heartbeat)
        self.app.router.add_get("/api/worker/nodes", self.api_worker_nodes)
        self.app.router.add_post("/api/worker/update", self.api_worker_trigger_update)
        self.app.router.add_post("/api/worker/rolling", self.api_worker_rolling_start)
        self.app.router.add_get("/api/worker/rolling/status", self.api_worker_rolling_status)
        self.app.router.add_post("/api/worker/restart", self.api_worker_restart)
        self.app.router.add_get("/api/worker/logs", self.api_worker_logs)
        self.app.router.add_get("/api/worker/manifest", self.api_worker_manifest)
        self.app.router.add_get("/api/worker/models/list", self.api_worker_models_list)
        self.app.router.add_get("/api/worker/models/manifest/{model_name}", self.api_worker_models_manifest)
        self.app.router.add_get("/api/worker/models/file/{model_name}/{file_path:.+}", self.api_worker_models_file)
        self.app.router.add_get("/api/worker/models/download/{model_name}", self.api_worker_models_download)

        # パスワード認証 & Bot管理 & クレデンシャル管理 API (OSS動的管理)
        self.app.router.add_post("/api/auth/login_password", self.api_auth_login_password)
        self.app.router.add_get("/api/system/bots", self.api_system_get_bots)
        self.app.router.add_post("/api/system/bots", self.api_system_save_bot)
        self.app.router.add_delete("/api/system/bots/{idx}", self.api_system_delete_bot)
        self.app.router.add_post("/api/system/bots/{idx}/toggle", self.api_system_toggle_bot)
        self.app.router.add_get("/api/system/credentials", self.api_system_get_credentials)
        self.app.router.add_post("/api/system/credentials", self.api_system_save_credentials)
        self.app.router.add_post("/api/system/credentials/test", self.api_system_test_credentials)
        self.app.router.add_post("/api/system/tunnel/restart", self.api_system_tunnel_restart)
        self.app.router.add_post("/api/system/tunnel/stop", self.api_system_tunnel_stop)
        self.app.router.add_post("/api/system/alert/test", self.api_system_alert_test)
        self.app.router.add_get("/api/system/tier_routing", self.api_system_get_tier_routing)
        self.app.router.add_post("/api/system/tier_routing", self.api_system_save_tier_routing)
        self.app.router.add_get("/api/worker/ws", self.api_worker_ws)

        # HA (High Availability) & Standby 同期 API
        self.app.router.add_get("/api/ha/db_snapshot", self.api_ha_db_snapshot)
        self.app.router.add_get("/api/ha/code_snapshot", self.api_ha_code_snapshot)
        self.app.router.add_get("/api/ha/status", self.api_ha_status)
        self.app.router.add_post("/api/ha/broadcast_update", self.api_ha_broadcast_update)
        self.app.router.add_get("/api/system/manager_nodes", self.api_system_get_manager_nodes)
        self.app.router.add_post("/api/system/manager_nodes/priority", self.api_system_set_manager_nodes_priority)
        self.app.router.add_post("/api/system/manager_nodes/revoke", self.api_system_revoke_manager_node)
        self.app.router.add_delete("/api/system/manager_nodes/{node_id}", self.api_system_delete_manager_node)
        self.app.router.add_post("/api/worker/kick", self.api_worker_kick_node)
        self.app.router.add_post("/api/worker/broadcast_update", self.api_worker_broadcast_update)

        # テスト・シミュレーション API (ローカルまたは管理者用)
        self.app.router.add_post("/api/test/simulate", self.api_test_simulate)

        # ヘルスチェック (監視・死活確認・Docker HEALTHCHECK 用)
        self.app.router.add_get("/health", self.handle_health)

        # 静的ファイル
        static_dir = Path(__file__).resolve().parent / "static"
        static_dir.mkdir(parents=True, exist_ok=True)
        self.app.router.add_static("/static", path=str(static_dir), name="static")

    def _is_wan_request(self, request: web.Request) -> bool:
        """
        リクエストが Cloudflare Tunnel (WAN / インターネット) 経由で到達したかを厳密判定。
        - CF-Connecting-IP, CF-Ray, CF-Visitor がある場合は確実に Cloudflare 経由
        - X-Forwarded-For があり、かつパブリックIPを含む場合
        - クライアントIPがプライベートIPでない場合
        """
        headers = request.headers
        if "CF-Connecting-IP" in headers or "CF-Ray" in headers or "CF-Visitor" in headers:
            return True

        # X-Forwarded-For の発信元IPを判定 (Nginxプロキシ経由の通信に対応 & 偽装ヘッダー防御)
        if "X-Forwarded-For" in headers:
            forwarded_ips = [ip.strip() for ip in headers["X-Forwarded-For"].split(",") if ip.strip()]
            if not forwarded_ips:
                return True
            # クライアント付与の先頭IPとNginx追記の末尾IPの双方を検証
            orig_ip = forwarded_ips[0]
            last_ip = forwarded_ips[-1]
            for ip_chk in (orig_ip, last_ip):
                # プライベート/ループバックIP以外が含まれていればWAN経由と判定
                if not (ip_chk in ("127.0.0.1", "::1", "localhost") or
                        ip_chk.startswith("192.168.") or
                        ip_chk.startswith("10.") or
                        ip_chk.startswith("172.")):
                    return True
            return False

        peername = request.transport.get_extra_info('peername') if request.transport else None
        client_ip = peername[0] if peername else (request.remote or "")

        # 宅内LAN IP (192.168.11.*), ループバック (127.0.0.1, ::1) の場合は LAN 直接通信
        if client_ip in ("127.0.0.1", "::1", "localhost") or client_ip.startswith("192.168."):
            return False

        # Docker 内部ネットワーク (172.x) の場合、CFヘッダーがなければローカルコンテナ間通信
        return bool("CF-Connecting-IP" in headers)

    def _is_local_request(self, request: web.Request) -> bool:
        """ローカルホストまたはLAN内部からのリクエストかを判定"""
        return not self._is_wan_request(request)

    @web.middleware
    async def _security_and_rate_limit_middleware(self, request: web.Request, handler):
        """
        セキュリティ境界防御 & CSRF防御 & WebUI API レート制限 & セキュリティヘッダー付与ミドルウェア
        """
        resp = await self._execute_security_and_rate_limit(request, handler)
        if isinstance(resp, web.StreamResponse):
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            resp.headers.setdefault("X-Frame-Options", "DENY")
            resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
            resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            if request.path.startswith("/static/"):
                resp.headers.setdefault("Cache-Control", "public, max-age=86400")
        return resp

    async def _execute_security_and_rate_limit(self, request: web.Request, handler):
        """
        セキュリティ境界防御 & CSRF防御 & WebUI API レート制限の内部ハンドラ
        1. WAN境界セキュリティ防御: Worker API / セットアップファイルのインターネット直接アクセス遮断
        2. CSRF防御: 状態変更系APIに対する Origin / Referer 検証
        3. レート制限: スライディングウィンドウ制御
        """
        path = request.path
        is_wan = self._is_wan_request(request)

        # 1. WAN境界セキュリティ防御: Worker 内部通信APIのインターネット直接アクセス遮断
        worker_sensitive_prefixes = (
            "/api/worker/",
        )
        if is_wan and any(path == p or path.startswith(p) for p in worker_sensitive_prefixes):
            # 例外1: WebUIダッシュボードの管理用API (/api/worker/nodes, /api/worker/logs, /api/worker/update等) は認証済み管理者なら許可
            if path in ("/api/worker/nodes", "/api/worker/logs", "/api/worker/update",
                        "/api/worker/rolling", "/api/worker/rolling/status", "/api/worker/restart"):
                user = self.auth.get_current_user(request)
                if not user:
                    return web.json_response({"error": "Forbidden: WAN access requires authentication"}, status=403)
            # 例外2: リモート Worker / クラスタ Worker 接続エンドポイント (/api/worker/ws, /api/worker/register, /api/worker/heartbeat, /api/worker/models/)
            # 外部 (Cloudflare Tunnel / インターネット) からの安全な接続のため、正当な共有トークン (WORKER_SHARED_SECRET) が【必須】
            elif path in ("/api/worker/ws", "/api/worker/register", "/api/worker/heartbeat") or path.startswith("/api/worker/models/"):
                if not self._verify_worker_auth(request):
                    client_ip = request.headers.get("CF-Connecting-IP") or request.remote or "unknown"
                    logger.warning(f"🚫 [Security] Blocked WAN worker request (missing or invalid shared token) to '{path}' from {client_ip}")
                    return web.json_response(
                        {"error": "Forbidden: Valid worker shared token is required for WAN access."},
                        status=403
                    )
            else:
                client_ip = request.headers.get("CF-Connecting-IP") or request.remote or "unknown"
                logger.warning(f"🚫 [Security] Blocked WAN access to worker-internal endpoint '{path}' from {client_ip}")
                return web.json_response(
                    {"error": "Forbidden: This endpoint is restricted to local LAN access only."},
                    status=403
                )

        # 2. CSRF防御: 状態変更系API (POST, PUT, DELETE) に対する Origin / Referer 検証 (Worker API除く)
        if is_wan and request.method in ("POST", "PUT", "DELETE") and path.startswith("/api/") and not path.startswith("/api/worker/"):
            origin = request.headers.get("Origin") or ""
            referer = request.headers.get("Referer") or ""
            allowed_hosts = {"localhost", "127.0.0.1"}
            req_host = (request.host.split(":")[0] if request.host else "").lower()
            if req_host:
                allowed_hosts.add(req_host)
            dash_url = getattr(self.config, "web_dashboard_url", "")
            if dash_url:
                try:
                    from urllib.parse import urlparse
                    d_host = urlparse(dash_url).hostname
                    if d_host:
                        allowed_hosts.add(d_host.lower())
                except Exception:
                    pass

            valid_csrf = False
            for check_url in (origin, referer):
                if check_url:
                    try:
                        from urllib.parse import urlparse
                        p_host = urlparse(check_url).hostname
                        if p_host and (p_host.lower() in allowed_hosts or p_host.startswith("192.168.") or p_host.startswith("10.") or p_host.startswith("172.")):
                            valid_csrf = True
                            break
                    except Exception:
                        pass

            if not (origin or referer):
                logger.warning(f"🚫 [CSRF] Blocked WAN request missing Origin and Referer headers to '{path}'")
                return web.json_response({"error": "Missing Origin or Referer header for cross-site verification."}, status=403)
            elif not valid_csrf:
                logger.warning(f"🚫 [CSRF] Blocked cross-origin request to '{path}' from Origin='{origin}', Referer='{referer}'")
                return web.json_response({"error": "Cross-Origin request forbidden."}, status=403)

        # 3. レート制限ミドルウェア
        if not path.startswith("/api/") or path.startswith("/api/worker/") or path == "/health":
            return await handler(request)

        client_key = request.remote or "unknown"
        user = self.auth.get_current_user(request)
        if user and "user_id" in user:
            client_key = f"user_{user['user_id']}"
        else:
            client_key = f"ip_{client_key}"

        now = time.time()
        window = 60.0

        is_heavy = any(path.startswith(p) for p in ("/api/preview", "/api/cache/purge", "/api/cache/scan", "/api/dict/import"))
        max_requests = 30 if is_heavy else 120
        bucket_key = f"{client_key}_{'heavy' if is_heavy else 'general'}"

        if bucket_key not in self._rate_limits:
            self._rate_limits[bucket_key] = deque()

        timestamps = self._rate_limits[bucket_key]
        while timestamps and timestamps[0] <= now - window:
            timestamps.popleft()

        if len(timestamps) >= max_requests:
            logger.warning(f"Rate limit exceeded for {bucket_key} on {path} ({len(timestamps)}/{max_requests} reqs/60s)")
            return web.json_response(
                {
                    "error": "Too Many Requests",
                    "message": "リクエスト頻度が高すぎます。しばらく待ってから再度お試しください。"
                },
                status=429,
                headers={"Retry-After": "60"}
            )

        timestamps.append(now)

        self._rate_limit_cleanup_counter += 1
        if self._rate_limit_cleanup_counter >= 100:
            self._rate_limit_cleanup_counter = 0
            to_del = [k for k, q in self._rate_limits.items() if not q or q[-1] <= now - window]
            for k in to_del:
                del self._rate_limits[k]

        resp = await handler(request)
        if path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
        return resp

    async def handle_health(self, request: web.Request) -> web.Response:
        """
        ヘルスチェックエンドポイント (Docker HEALTHCHECK / ロードバランサ / 死活監視用)
        Bot接続状態、レイテンシ、DB疎通、ノードプール状態、システムメモリを返却。
        """
        now = time.time()
        is_bot_ready = self.bot.is_ready() if self.bot else False
        latency_ms = round(self.bot.latency * 1000, 2) if (self.bot and self.bot.latency is not None) else None
        guilds_cnt = len(self.bot.guilds) if self.bot else 0

        # DB疎通チェック
        db_ok = False
        db_err = None
        try:
            async with aiosqlite.connect(self.db.db_path) as db:
                async with db.execute("SELECT 1") as cur:
                    await cur.fetchone()
            db_ok = True
        except Exception as e:
            db_err = str(e)

        # ノードプール状態 (全エンジンプールから集約)
        worker_nodes = []
        pools = [
            getattr(self.tts_manager, "voicevox_pool", None),
            getattr(self.tts_manager, "sharevox_pool", None),
            getattr(self.tts_manager, "coeiroink_pool", None),
        ]
        seen_nodes = set()
        for pool in pools:
            if not pool:
                continue
            for n in getattr(pool, "nodes", []):
                if n.id in seen_nodes:
                    continue
                seen_nodes.add(n.id)
                worker_nodes.append({
                    "id": n.id,
                    "machine_name": n.machine_name,
                    "engine_name": n.engine_name,
                    "url": n.url,
                    "is_healthy": bool(n.is_healthy),
                    "is_available": bool(n.is_available),
                    "is_external": bool(n.is_external),
                    "draining": bool(n.draining),
                })

        # メモリ情報
        mem_info = None
        if hasattr(self.tts_manager, "resource_manager") and self.tts_manager.resource_manager:
            mem_info = self.tts_manager.resource_manager.get_system_memory_info()

        has_configured_bots = bool(self.config.get_discord_tokens())
        if not has_configured_bots:
            # 初回起動・トークン0件待機状態: WebUIとDBが健全であればセットアップ待機(200 OK)
            is_healthy = db_ok
            overall_status = "waiting_setup" if db_ok else "unhealthy"
        else:
            is_healthy = is_bot_ready and db_ok
            overall_status = "ok" if is_healthy else ("degraded" if db_ok else "unhealthy")

        status_code = 200 if is_healthy else 503

        data = {
            "status": overall_status,
            "timestamp": now,
            "version": BUILD_VERSION,
            "build_date": BUILD_DATE,
            "bot": {
                "is_ready": is_bot_ready,
                "latency_ms": latency_ms,
                "guilds_count": guilds_cnt,
            },
            "database": {
                "status": "connected" if db_ok else "error",
                "error": db_err
            },
            "workers": {
                "count": len(worker_nodes),
                "nodes": worker_nodes
            },
            "memory": mem_info
        }
        return web.json_response(data, status=status_code)


    # --- 認証 & 権限ヘルパー ---

    def _require_auth(self, request: web.Request) -> Dict[str, Any]:
        user = self.auth.get_current_user(request)
        if not user:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "Unauthorized. Please log in with Discord."}),
                content_type="application/json"
            )
        return user

    async def _is_admin_user(self, request: web.Request) -> bool:
        """ログインユーザーが管理者権限を持つか判定 (パスワード認証の管理者 または Discordギルド管理者)"""
        user = self.auth.get_current_user(request)
        if not user or "user_id" not in user:
            return False
        # パスワード認証による管理者フラグ、またはID 0
        if user.get("is_admin") or user.get("user_id") == 0:
            return True
        user_id = user["user_id"]
        admin_user_ids = getattr(self.config, "admin_user_ids", []) or []
        if user_id in admin_user_ids:
            return True
        if self.bot and getattr(self.bot, "guilds", None):
            for guild in self.bot.guilds:
                member = await self._get_or_fetch_member(guild, user_id)
                if member and (member.guild_permissions.administrator or guild.owner_id == user_id):
                    return True
        return False

    async def _require_admin(self, request: web.Request) -> Dict[str, Any]:
        """管理者権限を必須とする認可ガード (一般ユーザーのアクセスを403でブロック)"""
        user = self._require_auth(request)
        if not await self._is_admin_user(request):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Forbidden: Administrator permission required."}),
                content_type="application/json"
            )
        return user

    async def _get_guild_and_perms(self, request: web.Request, guild_id_param: Optional[str] = None) -> Tuple[Any, Dict[str, bool]]:
        """指定ギルド（または所属ギルド）と、そのギルド内でのユーザー権限を取得・検証"""
        user = self._require_auth(request)
        user_id = user["user_id"]

        target_guild_id: Optional[int] = None
        if guild_id_param:
            try:
                target_guild_id = int(guild_id_param)
            except (ValueError, TypeError):
                pass

        guild: Optional[discord.Guild] = None
        if target_guild_id:
            guild = self.bot.get_guild(target_guild_id)
        else:
            # 指定がない場合はユーザーが所属するBot参加ギルドの先頭を採用
            for g in self.bot.guilds:
                m = await self._get_or_fetch_member(g, user_id)
                if m:
                    guild = g
                    break

        if not guild:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "Guild not found or bot is not in the guild"}),
                content_type="application/json"
            )

        member = await self._get_or_fetch_member(guild, user_id)

        if not member:
            raise web.HTTPForbidden(
                text=json.dumps({"error": "User is not a member of this guild"}),
                content_type="application/json"
            )

        is_bot_admin = user_id in getattr(self.config, "admin_user_ids", [])
        is_admin = is_bot_admin or member.guild_permissions.administrator or (guild.owner_id == user_id)
        guild_settings = await self.db.get_guild_settings(guild.id, self.config)
        admin_role_id = guild_settings.get("admin_role_id", 0)

        has_role = is_admin
        if admin_role_id and admin_role_id > 0:
            if any(r.id == admin_role_id for r in member.roles):
                has_role = True

        perms = {
            "is_admin": is_admin,
            "has_role": has_role
        }
        return guild, perms

    # --- ページハンドラ ---

    async def handle_index(self, request: web.Request) -> web.Response:
        template_path = Path(__file__).resolve().parent / "templates" / "index.html"
        if not template_path.exists():
            return web.Response(text="index.html not found", status=500)

        html = template_path.read_text(encoding="utf-8")
        user = self.auth.get_current_user(request)

        # テンプレート内の簡易変数置換 (Reflected XSS 防護: 外部入力由来の文字列は埋め込まず boolean のみ展開)
        is_logged_in = "true" if user else "false"
        client_configured = "true" if self.auth.is_configured() else "false"
        user_json = json.dumps({"is_admin": bool(user.get("is_admin", False))}) if user else "null"

        # 静的アセットキャッシュバスター (ファイルのmtimeを付与してCloudflare等のエッジキャッシュを強制バイパス)
        app_js_path = Path(__file__).resolve().parent / "static" / "app.js"
        style_css_path = Path(__file__).resolve().parent / "static" / "style.css"
        js_mtime = int(app_js_path.stat().st_mtime) if app_js_path.exists() else 0
        css_mtime = int(style_css_path.stat().st_mtime) if style_css_path.exists() else 0
        asset_v = f"{BUILD_VERSION}-{max(js_mtime, css_mtime)}"

        worker_token = getattr(self.config, "worker_shared_secret", "").strip()

        html = html.replace("{{IS_LOGGED_IN}}", is_logged_in)
        html = html.replace("{{CLIENT_CONFIGURED}}", client_configured)
        html = html.replace("{{USER_DATA}}", user_json)
        html = html.replace("{{WORKER_TOKEN}}", worker_token)
        html = html.replace("{{BUILD_ID}}", BUILD_VERSION)
        html = html.replace("{{BUILD_DATE}}", BUILD_DATE)
        html = html.replace("{{ASSET_V}}", asset_v)
        html = html.replace("{{AGENT_VERSION}}", AGENT_VERSION)

        response = web.Response(text=html, content_type="text/html")
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-App-Build"] = BUILD_VERSION
        return response

    async def handle_setup_worker_sh(self, request: web.Request) -> web.Response:
        """Debian/Linux 用ワンライナー自動セットアップスクリプトを配信 (URL & トークン動的注入)"""
        script_path = Path(__file__).resolve().parent / "static" / "setup-worker.sh"
        if not script_path.exists():
            return web.Response(text="#!/bin/bash\necho 'Error: setup-worker.sh not found'\nexit 1\n", status=404, content_type="text/plain", charset="utf-8")
        content = script_path.read_text(encoding="utf-8").lstrip("\ufeff")
        master_url = self._get_master_base_url(request)
        token = getattr(self.config, "worker_shared_secret", "").strip()
        # MASTER_URL を動的置換 (http://localhost:8080 / http://localhost:8088 等あらゆるデフォルト値に対応)
        content = re.sub(r'MASTER_URL="\$\{TTSBOT_MASTER_URL:-[^}"]*\}"', f'MASTER_URL="${{TTSBOT_MASTER_URL:-{master_url}}}"', content)
        content = re.sub(r'WORKER_SECRET="\$\{TTSBOT_WORKER_SECRET:-[^}"]*\}"', f'WORKER_SECRET="${{TTSBOT_WORKER_SECRET:-{token}}}"', content)
        # 固定文字列置換も安全網として保持
        content = content.replace('MASTER_URL="${TTSBOT_MASTER_URL:-http://localhost:8080}"', f'MASTER_URL="${{TTSBOT_MASTER_URL:-{master_url}}}"')
        content = content.replace('MASTER_URL="${TTSBOT_MASTER_URL:-http://localhost:8088}"', f'MASTER_URL="${{TTSBOT_MASTER_URL:-{master_url}}}"')
        content = content.replace('WORKER_SECRET="${TTSBOT_WORKER_SECRET:-}"', f'WORKER_SECRET="${{TTSBOT_WORKER_SECRET:-{token}}}"')
        resp = web.Response(text=content, content_type="text/plain", charset="utf-8")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    async def handle_setup_worker_ps1(self, request: web.Request) -> web.Response:
        """Windows GPU 用ワンライナー自動セットアップスクリプトを配信 (URL & トークン動的注入)"""
        script_path = Path(__file__).resolve().parent / "static" / "setup-worker.ps1"
        if not script_path.exists():
            return web.Response(text="Write-Host 'Error: setup-worker.ps1 not found' -ForegroundColor Red\nexit 1\n", status=404, content_type="text/plain", charset="utf-8")
        content = script_path.read_text(encoding="utf-8").lstrip("\ufeff")
        master_url = self._get_master_base_url(request)
        token = getattr(self.config, "worker_shared_secret", "").strip()
        # $MasterUrl を動的置換 (8080 / 8088 等あらゆるデフォルト値に対応)
        content = re.sub(r'\$MasterUrl\s*=\s*if\s*\(\$env:TTSBOT_MASTER_URL\)\s*\{\s*\$env:TTSBOT_MASTER_URL\s*\}\s*else\s*\{\s*"[^"]*"\s*\}',
                         f'$MasterUrl  = if ($env:TTSBOT_MASTER_URL) {{ $env:TTSBOT_MASTER_URL }} else {{ "{master_url}" }}', content)
        content = re.sub(r'\$WorkerSecret\s*=\s*if\s*\(\$env:TTSBOT_WORKER_SECRET\)\s*\{\s*\$env:TTSBOT_WORKER_SECRET\s*\}\s*else\s*\{\s*"[^"]*"\s*\}',
                         f'$WorkerSecret = if ($env:TTSBOT_WORKER_SECRET) {{ $env:TTSBOT_WORKER_SECRET }} else {{ "{token}" }}', content)
        # 固定文字列置換も安全網として保持
        content = content.replace('$MasterUrl  = if ($env:TTSBOT_MASTER_URL) { $env:TTSBOT_MASTER_URL } else { "http://localhost:8080" }',
                                  f'$MasterUrl  = if ($env:TTSBOT_MASTER_URL) {{ $env:TTSBOT_MASTER_URL }} else {{ "{master_url}" }}')
        content = content.replace('$MasterUrl  = if ($env:TTSBOT_MASTER_URL) { $env:TTSBOT_MASTER_URL } else { "http://localhost:8088" }',
                                  f'$MasterUrl  = if ($env:TTSBOT_MASTER_URL) {{ $env:TTSBOT_MASTER_URL }} else {{ "{master_url}" }}')
        content = content.replace('$WorkerSecret = if ($env:TTSBOT_WORKER_SECRET) { $env:TTSBOT_WORKER_SECRET } else { "" }',
                                  f'$WorkerSecret = if ($env:TTSBOT_WORKER_SECRET) {{ $env:TTSBOT_WORKER_SECRET }} else {{ "{token}" }}')
        resp = web.Response(text=content, content_type="text/plain", charset="utf-8")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    def _get_master_base_url(self, request: web.Request) -> str:
        """アクセス元リクエストおよび設定から Master サーバーのベースURLを自動特定 (WAN / LAN 自動判別)"""
        # LAN内・ローカルからの直接アクセスの場合、リクエストされたHost/IPを最優先 (LAN直接通信・超低遅延化)
        if not self._is_wan_request(request):
            proto = request.headers.get("X-Forwarded-Proto") or request.scheme or "http"
            host = request.headers.get("Host") or request.host or "localhost:8088"
            return f"{proto}://{host}".rstrip("/")

        # WAN経由 (Cloudflare / インターネット) の場合、設定された外部公開URL (web_dashboard_url) または X-Forwarded-Host を優先
        if self.config.web_dashboard_url and self.config.web_dashboard_url.startswith("http"):
            return self.config.web_dashboard_url.rstrip("/")

        proto = "https" if ("https" in (request.headers.get("X-Forwarded-Proto") or request.headers.get("CF-Visitor", ""))) else (request.scheme or "http")
        host = (
            request.headers.get("X-Forwarded-Host") or
            request.headers.get("Host") or
            request.host or
            "localhost:8088"
        )
        return f"{proto}://{host}".rstrip("/")

    async def _ensure_ha_secret(self) -> str:
        """HA共有認証シークレットの取得（未設定時は自動生成＆永続化）"""
        ha_secret = self.config.ha_secret
        if not ha_secret:
            ha_secret = await self.db.get_credential("ha_secret")
        if not ha_secret:
            import secrets
            ha_secret = secrets.token_urlsafe(24)
            await self.db.set_credential("ha_secret", ha_secret, is_secret=True)
            self.config.ha_secret = ha_secret
            logger.info("🔐 Generated and saved default TTSBOT_HA_SECRET for cluster HA.")
        else:
            self.config.ha_secret = ha_secret
        return ha_secret

    async def handle_setup_standby_sh(self, request: web.Request) -> web.Response:
        """Linux/macOS 用 Standby (Slave) ワンライナー自動セットアップスクリプトを配信 (Master URL/Secret動的埋め込み)"""
        script_path = Path(__file__).resolve().parent / "static" / "setup-standby.sh"
        if not script_path.exists():
            return web.Response(text="#!/bin/bash\necho 'Error: setup-standby.sh not found'\nexit 1\n", status=404, content_type="text/plain", charset="utf-8")

        master_url = self._get_master_base_url(request)
        ha_secret = await self._ensure_ha_secret()

        content = script_path.read_text(encoding="utf-8")
        content = content.replace("__DEFAULT_MASTER_URL__", master_url)
        content = content.replace("__DEFAULT_HA_SECRET__", ha_secret)
        return web.Response(text=content, content_type="text/plain", charset="utf-8")

    async def handle_setup_standby_ps1(self, request: web.Request) -> web.Response:
        """Windows 用 Standby (Slave) ワンライナー自動セットアップスクリプトを配信 (Master URL/Secret動的埋め込み)"""
        script_path = Path(__file__).resolve().parent / "static" / "setup-standby.ps1"
        if not script_path.exists():
            return web.Response(text="Write-Host 'Error: setup-standby.ps1 not found' -ForegroundColor Red\nexit 1\n", status=404, content_type="text/plain", charset="utf-8")

        master_url = self._get_master_base_url(request)
        ha_secret = await self._ensure_ha_secret()

        content = script_path.read_text(encoding="utf-8")
        content = content.replace("__DEFAULT_MASTER_URL__", master_url)
        content = content.replace("__DEFAULT_HA_SECRET__", ha_secret)
        return web.Response(text=content, content_type="text/plain", charset="utf-8")

    async def handle_update_sh(self, request: web.Request) -> web.Response:
        """ワンライナー自動アップデートスクリプト update.sh を配信"""
        candidates = [
            Path(__file__).resolve().parent / "static" / "update.sh",
            Path("/app/update.sh"),
            Path("update.sh"),
            Path(__file__).resolve().parent.parent.parent / "update.sh",
            Path("/root/ttsbot/update.sh"),
        ]
        for p in candidates:
            if p.exists() and p.is_file():
                return web.Response(text=p.read_text(encoding="utf-8"), content_type="text/x-shellscript", charset="utf-8")
        return web.Response(text="#!/bin/bash\necho 'Error: update.sh not found'\nexit 1\n", status=404, content_type="text/plain", charset="utf-8")

    async def handle_docker_compose_yml(self, request: web.Request) -> web.Response:
        """Standby/Worker 用 docker-compose.yml を配信"""
        candidates = [
            Path(__file__).resolve().parent / "static" / "docker-compose.yml",
            Path("/app/docker-compose.yml"),
            Path("docker-compose.yml"),
            Path(__file__).resolve().parent.parent.parent / "docker-compose.yml"
        ]
        for p in candidates:
            if p.exists():
                return web.Response(text=p.read_text(encoding="utf-8"), content_type="text/yaml", charset="utf-8")
        return web.Response(text="# docker-compose.yml not found\n", status=404, content_type="text/plain")

    async def handle_docker_compose_slave_yml(self, request: web.Request) -> web.Response:
        """Slave (Standby) 専用 docker-compose.slave.yml を配信"""
        candidates = [
            Path(__file__).resolve().parent / "static" / "docker-compose.slave.yml",
            Path("/app/docker-compose.slave.yml"),
            Path("docker-compose.slave.yml"),
            Path(__file__).resolve().parent.parent.parent / "docker-compose.slave.yml"
        ]
        for p in candidates:
            if p.exists():
                return web.Response(text=p.read_text(encoding="utf-8"), content_type="text/yaml", charset="utf-8")
        return web.Response(text="# docker-compose.slave.yml not found\n", status=404, content_type="text/plain")

    async def handle_portainer_stack_yml(self, request: web.Request) -> web.Response:
        """Portainer 用 portainer-stack.yml を配信"""
        candidates = [
            Path(__file__).resolve().parent / "static" / "portainer-stack.yml",
            Path("/app/portainer-stack.yml"),
            Path("portainer-stack.yml"),
            Path(__file__).resolve().parent.parent.parent / "portainer-stack.yml"
        ]
        for p in candidates:
            if p.exists():
                return web.Response(text=p.read_text(encoding="utf-8"), content_type="text/yaml", charset="utf-8")
        return web.Response(text="# portainer-stack.yml not found\n", status=404, content_type="text/plain")

    async def handle_volunteer_file(self, request: web.Request) -> web.Response:
        """ボランティアツールの個別スクリプトファイルを配信 (CWE-22 / CodeQL Path Injection 対策済み)"""
        VOLUNTEER_SAFE_FILES = {
            "run_volunteer.bat": "run_volunteer.bat",
            "run_volunteer_gui.ps1": "run_volunteer_gui.ps1",
            "run_volunteer.sh": "run_volunteer.sh",
            "volunteer_worker.py": "volunteer_worker.py",
            "README.md": "README.md",
        }
        raw_name = request.path.split("/")[-1]
        safe_filename = VOLUNTEER_SAFE_FILES.get(raw_name)
        if not safe_filename:
            return web.Response(text="File not found", status=404)

        candidate_dirs = [
            (Path(__file__).resolve().parent / "static" / "volunteer"),
            Path("/app/worker"),
            (Path(__file__).resolve().parent.parent.parent / "worker"),
        ]
        file_path = None
        for base_dir in candidate_dirs:
            safe_p = validate_safe_path(base_dir, safe_filename)
            if safe_p and safe_p.is_file():
                file_path = safe_p
                break

        if not file_path:
            return web.Response(text="File not found", status=404)

        if safe_filename.endswith((".sh", ".ps1", ".bat")):
            content = file_path.read_text(encoding="utf-8")
            master_base_url = self._get_master_base_url(request)
            ws_url = master_base_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/api/worker/ws"
            worker_token = getattr(self.config, "worker_shared_secret", "").strip()

            content = content.replace("__DEFAULT_SERVER_WS_URL__", ws_url)
            content = content.replace("__DEFAULT_WORKER_TOKEN__", worker_token)
            content = content.lstrip("\ufeff")
            return web.Response(text=content, content_type="text/plain", charset="utf-8")

        ct = "text/plain" if safe_filename.endswith((".py", ".md")) else "application/octet-stream"
        return web.Response(body=file_path.read_bytes(), content_type=ct, charset="utf-8" if ct.startswith("text") else None)

    async def handle_volunteer_zip(self, request: web.Request) -> web.Response:
        """Windows向けボランティアツール一式 (zip) をオンザフライ生成して配信"""
        import io
        import zipfile
        worker_dir = Path(__file__).resolve().parent / "static" / "volunteer"
        if not worker_dir.exists():
            worker_dir = Path("/app/worker")
        if not worker_dir.exists():
            worker_dir = Path(__file__).resolve().parent.parent.parent / "worker"

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in worker_dir.glob("*"):
                if item.is_file() and not item.name.startswith((".", "config.json")):
                    zf.write(item, arcname=item.name)
        buf.seek(0)
        headers = {
            "Content-Disposition": 'attachment; filename="ttsbot-volunteer-worker.zip"'
        }
        return web.Response(body=buf.read(), content_type="application/zip", headers=headers)

    def _verify_ha_auth(self, request: web.Request) -> bool:
        """HA API 共有シークレット認証 (Master-Slave間)"""
        expected = (self.config.ha_secret or "").strip()
        if not expected:
            logger.warning("[HA] Auth rejected: TTSBOT_HA_SECRET is not configured on Master node.")
            return False

        auth_header = request.headers.get("Authorization", "")
        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        if not token:
            token = request.headers.get("X-HA-Secret", "").strip()

        if not token:
            return False
        return secrets.compare_digest(token, expected)

    async def api_ha_status(self, request: web.Request) -> web.Response:
        """HA 状態確認 API (Slave からの死活 & メタデータ照会 & ハートビート記録)"""
        if not self._verify_ha_auth(request):
            return web.json_response({"error": "Unauthorized HA request. Invalid or unconfigured HA secret."}, status=403)

        node_id = request.headers.get("X-HA-Node-ID", "").strip()
        hostname = request.headers.get("X-HA-Hostname", "").strip() or "Slave Node"
        client_ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote or "unknown"
        user_agent = request.headers.get("User-Agent", "")

        local_ip = request.headers.get("X-HA-Local-IP", "").strip()
        docker_version = request.headers.get("X-HA-Docker-Version", "").strip()
        container_hostname = request.headers.get("X-HA-Container-Hostname", "").strip()
        is_direct_str = request.headers.get("X-HA-Direct-Verified", "").strip().lower()
        is_direct_verified = is_direct_str in ("true", "1", "yes")
        direct_url = request.headers.get("X-HA-Direct-Url", "").strip()

        p_raw = request.headers.get("X-HA-Priority", "").strip()
        req_p = int(p_raw) if (p_raw and p_raw.isdigit()) else None
        cores_raw = request.headers.get("X-HA-Cpu-Cores", "1").strip()
        cpu_cores = int(cores_raw) if (cores_raw and cores_raw.isdigit()) else 1
        mem_raw = request.headers.get("X-HA-Memory-Gb", "0.0").strip()
        try:
            mem_gb = float(mem_raw)
        except ValueError:
            mem_gb = 0.0
        os_name = request.headers.get("X-HA-Os", "").strip()

        if node_id and (node_id in self.ha_revoked_nodes or self.ha_slaves.get(node_id, {}).get("status") == "revoked"):
            logger.warning(f"🛑 [HA] Status check from REVOKED Slave '{node_id}' ({hostname} @ {client_ip}). Sending 410 Gone.")
            self._record_ha_slave_heartbeat(
                node_id, hostname, client_ip, is_sync=False, user_agent=user_agent,
                cpu_cores=cpu_cores, memory_gb=mem_gb, os_name=os_name, requested_priority=req_p,
                local_ip=local_ip, docker_version=docker_version, is_direct_verified=is_direct_verified,
                direct_url=direct_url, container_hostname=container_hostname
            )
            return web.json_response({
                "action": "terminate",
                "error": "This Manager Slave node has been revoked and decommissioned by Master.",
                "revoked": True,
                "node_id": node_id
            }, status=410)

        if node_id:
            self._record_ha_slave_heartbeat(
                node_id, hostname, client_ip, is_sync=False, user_agent=user_agent,
                cpu_cores=cpu_cores, memory_gb=mem_gb, os_name=os_name, requested_priority=req_p,
                local_ip=local_ip, docker_version=docker_version, is_direct_verified=is_direct_verified,
                direct_url=direct_url, container_hostname=container_hostname
            )

        db_path = self.config.db_path
        db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
        db_mtime = os.path.getmtime(db_path) if os.path.exists(db_path) else 0

        active_bots = 1
        if self.bot_pool and hasattr(self.bot_pool, "get_active_bot_count"):
            active_bots = self.bot_pool.get_active_bot_count()

        slave_entry = self.ha_slaves.get(node_id, {})
        assigned_p = slave_entry.get("priority", 1)
        online_standbys = len([s for s in self.ha_slaves.values() if s.get("status") == "online"])

        now_ts = time.time()
        cluster_nodes = []
        for nid, s in self.ha_slaves.items():
            if nid != node_id and (now_ts - s.get("last_seen", 0)) < 120.0 and nid not in self.ha_revoked_nodes and s.get("status") != "revoked":
                cluster_nodes.append({
                    "node_id": nid,
                    "hostname": s.get("hostname", "unknown"),
                    "ip": s.get("ip", "unknown"),
                    "port": 8088,
                    "priority": s.get("priority", 1)
                })

        # 更新中フラグ (.ha_updating) の確認 (HA 相手ノードの誤昇格を防止)
        data_dir = getattr(self.config, "data_dir", None) or Path(getattr(self.config, "db_path", "/app/data/bot.db")).parent
        updating_flag = Path(data_dir) / ".ha_updating"
        is_updating = updating_flag.exists()

        code_hash = self._get_code_hash()
        host_info = get_host_system_info()
        master_port = int(os.getenv("HOST_WEB_PORT", getattr(self.config, "web_port", 8088)) or 8088)

        force_target = False
        if node_id and hasattr(self, "ha_slave_force_update_nodes"):
            if (now_ts - self.ha_slave_force_update_nodes.get(node_id, 0.0)) < 120.0:
                force_target = True

        return web.json_response({
            "status": "ok",
            "role": "master",
            "is_active": True,
            "master_instance_id": getattr(self, "master_instance_id", "master_primary"),
            "master_hostname": host_info.get("hostname") or socket.gethostname(),
            "master_container_hostname": host_info.get("container_hostname") or socket.gethostname(),
            "master_os": host_info.get("os_name", "Linux"),
            "master_docker_version": host_info.get("docker_version", "N/A"),
            "master_local_ips": host_info.get("local_ips", []),
            "master_port": master_port,
            "is_updating": is_updating,
            "updating": is_updating,
            "active_bots": active_bots,
            "assigned_priority": assigned_p,
            "priority": assigned_p,
            "total_standbys": online_standbys,
            "recommended_standbys": 2,
            "cluster_nodes": cluster_nodes,
            "db_size_bytes": db_size,
            "db_mtime": db_mtime,
            "timestamp": time.time(),
            "version": AGENT_VERSION,
            "code_hash": code_hash,
            "force_update": ((now_ts - self.ha_force_update_time) < 120.0) or force_target
        })

    def _get_code_hash(self) -> str:
        """src/ 配下の Python コードのハッシュ値を計算 (キャッシュ付き)"""
        now = time.time()
        if self._code_hash_cache and (now - self._code_hash_cache_time) < 10.0:
            return self._code_hash_cache

        try:
            src_dir = Path(__file__).resolve().parent.parent  # /app/src
            hasher = hashlib.sha256()
            for p in sorted(src_dir.rglob("*.py")):
                if "__pycache__" in str(p):
                    continue
                try:
                    stat = p.stat()
                    hasher.update(p.name.encode())
                    hasher.update(str(stat.st_mtime).encode())
                    hasher.update(str(stat.st_size).encode())
                except Exception:
                    pass
            self._code_hash_cache = hasher.hexdigest()
            self._code_hash_cache_time = now
            return self._code_hash_cache
        except Exception:
            return "unknown_code_hash"

    async def api_ha_db_snapshot(self, request: web.Request) -> web.Response:
        """Slave 同期用 SQLite 完全整合性バックアップ (gzip 圧縮) 配信"""
        if not self._verify_ha_auth(request):
            return web.json_response({"error": "Unauthorized HA request. Invalid or unconfigured HA secret."}, status=403)

        node_id = request.headers.get("X-HA-Node-ID", "").strip()
        hostname = request.headers.get("X-HA-Hostname", "").strip() or "Slave Node"
        client_ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote or "unknown"
        user_agent = request.headers.get("User-Agent", "")

        cores_raw = request.headers.get("X-HA-Cpu-Cores", "1").strip()
        cpu_cores = int(cores_raw) if (cores_raw and cores_raw.isdigit()) else 1
        mem_raw = request.headers.get("X-HA-Memory-Gb", "0.0").strip()
        try:
            mem_gb = float(mem_raw)
        except ValueError:
            mem_gb = 0.0
        os_name = request.headers.get("X-HA-Os", "").strip()

        if node_id and (node_id in self.ha_revoked_nodes or self.ha_slaves.get(node_id, {}).get("status") == "revoked"):
            logger.warning(f"🛑 [HA] Snapshot sync from REVOKED Slave '{node_id}' ({hostname} @ {client_ip}). Sending 410 Gone.")
            self._record_ha_slave_heartbeat(
                node_id, hostname, client_ip, is_sync=False, user_agent=user_agent,
                cpu_cores=cpu_cores, memory_gb=mem_gb, os_name=os_name
            )
            return web.json_response({
                "action": "terminate",
                "error": "This Manager Slave node has been revoked and decommissioned by Master.",
                "revoked": True,
                "node_id": node_id
            }, status=410)

        if node_id:
            self._record_ha_slave_heartbeat(
                node_id, hostname, client_ip, is_sync=True, user_agent=user_agent,
                cpu_cores=cpu_cores, memory_gb=mem_gb, os_name=os_name
            )

        db_path = self.config.db_path
        if not os.path.exists(db_path):
            return web.json_response({"error": "Database file not found on Master node"}, status=404)

        loop = asyncio.get_running_loop()

        def _generate_compressed_snapshot():
            import sqlite3, gzip, hashlib, tempfile
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_f:
                tmp_path = tmp_f.name
            try:
                # Read-only URI で WAL モード下でもアトミックバックアップ
                src_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                dst_conn = sqlite3.connect(tmp_path)
                src_conn.backup(dst_conn)
                dst_conn.close()
                src_conn.close()

                with open(tmp_path, "rb") as f:
                    raw_data = f.read()

                sha256 = hashlib.sha256(raw_data).hexdigest()
                compressed = gzip.compress(raw_data, compresslevel=6)
                return compressed, sha256, len(raw_data)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

        try:
            compressed_data, sha256_hash, raw_size = await loop.run_in_executor(None, _generate_compressed_snapshot)

            # If-None-Match による帯域節約 (変更なしなら 304)
            if_none_match = request.headers.get("If-None-Match", "").strip('"')
            if if_none_match == sha256_hash:
                return web.Response(status=304, headers={"ETag": f'"{sha256_hash}"'})

            headers = {
                "Content-Type": "application/x-sqlite3-gzip",
                "Content-Disposition": 'attachment; filename="bot.db.gz"',
                "ETag": f'"{sha256_hash}"',
                "X-DB-Hash": sha256_hash,
                "X-DB-Raw-Size": str(raw_size),
                "X-DB-Timestamp": str(int(time.time())),
                "Cache-Control": "no-cache"
            }
            return web.Response(body=compressed_data, headers=headers)
        except Exception as e:
            logger.error(f"[HA] Failed to generate DB snapshot: {e}", exc_info=True)
            return web.json_response({"error": f"Failed to generate DB snapshot: {str(e)}"}, status=500)

    async def api_ha_code_snapshot(self, request: web.Request) -> web.Response:
        """Slave 自動同期用 ソースコード完全アーカイブ (gzip tarball) 配信"""
        if not self._verify_ha_auth(request):
            return web.json_response({"error": "Unauthorized HA request. Invalid or unconfigured HA secret."}, status=403)

        code_hash = self._get_code_hash()
        if request.headers.get("If-None-Match", "").strip('"') == code_hash:
            return web.Response(status=304, headers={"ETag": f'"{code_hash}"'})

        loop = asyncio.get_running_loop()

        def _generate_code_tarball():
            src_dir = Path(__file__).resolve().parent.parent  # /app/src
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                def _filter(tarinfo):
                    # __pycache__ や .pyc 等の一時・バイナリファイルを除外
                    if "__pycache__" in tarinfo.name or tarinfo.name.endswith(".pyc") or tarinfo.name.endswith(".pyo"):
                        return None
                    return tarinfo
                tar.add(str(src_dir), arcname="src", filter=_filter)
            return buf.getvalue()

        try:
            tar_data = await loop.run_in_executor(None, _generate_code_tarball)
            headers = {
                "Content-Type": "application/x-gzip",
                "Content-Disposition": 'attachment; filename="src_code.tar.gz"',
                "ETag": f'"{code_hash}"',
                "X-Code-Hash": code_hash,
                "Cache-Control": "no-cache"
            }
            return web.Response(body=tar_data, headers=headers)
        except Exception as e:
            logger.error(f"[HA] Failed to generate code snapshot: {e}", exc_info=True)
            return web.json_response({"error": f"Failed to generate code snapshot: {str(e)}"}, status=500)

    async def api_ha_broadcast_update(self, request: web.Request) -> web.Response:
        """Standby Slave ノードへ即時自己アップデートを指示する (HA Secret または 管理者権限 または プライベート/ローカルネットワーク専用)"""
        authorized = False
        if self._verify_ha_auth(request):
            authorized = True
        else:
            remote_ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote or ""
            try:
                ip_obj = ipaddress.ip_address(remote_ip)
                if ip_obj.is_private or ip_obj.is_loopback:
                    authorized = True
            except ValueError:
                pass

        if not authorized:
            await self._require_admin(request)

        target_node_id = None
        if request.can_read_body:
            try:
                body = await request.json()
                if isinstance(body, dict):
                    target_node_id = body.get("node_id")
            except Exception:
                pass

        if target_node_id:
            if not hasattr(self, "ha_slave_force_update_nodes"):
                self.ha_slave_force_update_nodes = {}
            self.ha_slave_force_update_nodes[target_node_id] = time.time()
            logger.info(f"📢 [HA Update] Targeted auto-update signal to HA Standby node '{target_node_id}'.")
            return web.json_response({"status": "ok", "message": f"HA Standby update targeted to {target_node_id}."})
        else:
            self.ha_force_update_time = time.time()
            logger.info("📢 [HA Update] Broadcasted auto-update signal to all HA Standby Slave nodes.")
            return web.json_response({"status": "ok", "message": "HA Standby update broadcasted."})

    async def api_system_get_manager_nodes(self, request: web.Request) -> web.Response:
        """Master-Slave HA 構成のノード一覧と死活状況を取得 (管理者のみ)"""
        await self._require_admin(request)
        now = time.time()
        active_bots = self.bot_pool.get_active_bot_count() if (self.bot_pool and hasattr(self.bot_pool, "get_active_bot_count")) else 1

        host_info = get_host_system_info()
        local_ips = host_info.get("local_ips", [])
        primary_local_ip = next((ip for ip in local_ips if not ip.startswith("172.")), local_ips[0] if local_ips else "")
        master_port = int(os.getenv("HOST_WEB_PORT", getattr(self.config, "web_port", 8088)) or 8088)

        master_info = {
            "node_id": "master_primary",
            "instance_id": getattr(self, "master_instance_id", "master_primary"),
            "role": "master",
            "hostname": host_info.get("hostname") or socket.gethostname(),
            "container_hostname": host_info.get("container_hostname") or socket.gethostname(),
            "os_name": host_info.get("os_name", "Linux"),
            "kernel": host_info.get("kernel", ""),
            "docker_version": host_info.get("docker_version", "N/A"),
            "ip": request.host.split(":")[0] if request.host else "127.0.0.1",
            "local_ip": primary_local_ip,
            "local_ips": local_ips,
            "port": master_port,
            "status": "active",
            "status_display": "稼働中 (Active Master)",
            "active_bots": active_bots,
            "version": AGENT_VERSION,
            "is_current": True,
            "uptime_seconds": int(now - getattr(self, "_start_time", now))
        }

        slaves_list = []
        for node_id, slave in list(self.ha_slaves.items()):
            last_seen = slave.get("last_seen", 0)
            is_revoked = node_id in self.ha_revoked_nodes or slave.get("status") == "revoked"
            # 60秒以上アクセスがなければ offline 判定
            is_online = (now - last_seen) < 60.0 and not is_revoked
            status = "revoked" if is_revoked else ("online" if is_online else "offline")
            status_display = "停止・除外済 (Revoked)" if is_revoked else ("待機中 (Standby)" if is_online else "切断 (Offline)")

            priority = slave.get("priority", 1)
            cpu_cores = slave.get("cpu_cores", 1)
            memory_gb = slave.get("memory_gb", 0.0)
            os_name = slave.get("os_name", "")
            docker_ver = slave.get("docker_version", "")
            local_ip = slave.get("local_ip", "")
            is_direct = bool(slave.get("is_direct_verified", False))
            direct_url = slave.get("direct_url", "")

            slaves_list.append({
                "node_id": node_id,
                "hostname": slave.get("hostname", "unknown"),
                "container_hostname": slave.get("container_hostname", ""),
                "ip": slave.get("ip", "unknown"),
                "local_ip": local_ip,
                "is_direct_verified": is_direct,
                "direct_url": direct_url,
                "priority": priority,
                "priority_display": f"Priority {priority}" + (" (第1待機系・最速昇格)" if priority == 1 else f" (第{priority}待機系)"),
                "cpu_cores": cpu_cores,
                "memory_gb": memory_gb,
                "os_name": os_name,
                "docker_version": docker_ver,
                "spec_display": f"{cpu_cores} Cores / {memory_gb:.1f} GB RAM" if memory_gb > 0 else f"{cpu_cores} Cores",
                "last_seen": last_seen,
                "last_seen_ago": int(now - last_seen) if last_seen > 0 else -1,
                "last_sync": slave.get("last_sync", 0),
                "last_sync_ago": int(now - slave.get("last_sync", 0)) if slave.get("last_sync", 0) > 0 else -1,
                "status": status,
                "status_display": status_display,
                "user_agent": slave.get("user_agent", "")
            })

        # オンライン優先、その中で Priority 昇順 (1, 2, 3...)
        slaves_list.sort(key=lambda s: (
            0 if s["status"] == "online" else (1 if s["status"] == "offline" else 2),
            s["priority"],
            -s["last_seen"]
        ))

        return web.json_response({
            "status": "ok",
            "master": master_info,
            "slaves": slaves_list,
            "ha_secret_configured": bool(self.config.ha_secret),
            "total_nodes": 1 + len(slaves_list),
            "recommended_standbys": 2
        })

    async def api_system_set_manager_nodes_priority(self, request: web.Request) -> web.Response:
        """Manager Slave ノードの Priority (昇格優先度) を更新・整列 (管理者のみ)"""
        await self._require_admin(request)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        action = data.get("action", "")
        if action == "auto_spec":
            # スペック順に自動整列 (RAM大 > CPUコア大 > 先着順)
            valid_slaves = [
                (nid, s) for nid, s in self.ha_slaves.items()
                if nid not in self.ha_revoked_nodes and s.get("status") != "revoked"
            ]
            valid_slaves.sort(key=lambda item: (
                -(item[1].get("memory_gb") or 0.0),
                -(item[1].get("cpu_cores") or 1),
                item[1].get("first_seen") or 0.0
            ))
            for idx, (nid, s) in enumerate(valid_slaves, start=1):
                s["priority"] = idx
            self._save_ha_nodes()
            return web.json_response({
                "status": "ok",
                "message": "スペック順 (RAM・CPU大) に優先度を自動整列しました。"
            })

        # 一括指定
        node_priorities = data.get("node_priorities")
        if node_priorities and isinstance(node_priorities, list):
            for item in node_priorities:
                nid = str(item.get("node_id", "")).strip()
                p = item.get("priority")
                if nid in self.ha_slaves and isinstance(p, int) and p > 0:
                    self.ha_slaves[nid]["priority"] = p
            self._save_ha_nodes()
            return web.json_response({"status": "ok", "message": "優先度を更新しました。"})

        # 単一指定
        node_id = str(data.get("node_id", "")).strip()
        priority = data.get("priority")
        if node_id and node_id in self.ha_slaves and isinstance(priority, int) and priority > 0:
            self.ha_slaves[node_id]["priority"] = priority
            self._save_ha_nodes()
            return web.json_response({
                "status": "ok",
                "message": f"ノード '{node_id}' の優先度を Priority {priority} に更新しました。"
            })

        return web.json_response({"error": "node_id and valid priority (positive integer) or action is required"}, status=400)

    async def api_system_revoke_manager_node(self, request: web.Request) -> web.Response:
        """指定した Slave Node を除外・自律停止対象に登録 (管理者のみ)"""
        await self._require_admin(request)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        node_id = str(data.get("node_id", "")).strip()
        if not node_id:
            return web.json_response({"error": "node_id is required"}, status=400)

        self.ha_revoked_nodes.add(node_id)
        if node_id in self.ha_slaves:
            self.ha_slaves[node_id]["status"] = "revoked"
            self.ha_slaves[node_id]["revoked_at"] = time.time()
        self._save_ha_nodes()

        logger.warning(f"🛑 [HA] Manager Slave '{node_id}' has been marked as REVOKED by admin. Next heartbeat will return HTTP 410 Gone.")
        return web.json_response({
            "status": "ok",
            "message": f"Slave '{node_id}' を除外・停止対象に登録しました。次回ハートビート時に自律停止指令 (410 Gone) が送出されます。",
            "node_id": node_id
        })

    async def api_system_delete_manager_node(self, request: web.Request) -> web.Response:
        """指定した Slave Node の登録履歴を完全に削除 (管理者のみ)"""
        await self._require_admin(request)
        node_id = request.match_info.get("node_id", "").strip()
        if not node_id:
            return web.json_response({"error": "node_id is required"}, status=400)

        removed = False
        if node_id in self.ha_slaves:
            del self.ha_slaves[node_id]
            removed = True
        if node_id in self.ha_revoked_nodes:
            self.ha_revoked_nodes.remove(node_id)
            removed = True

        self._save_ha_nodes()
        return web.json_response({
            "status": "ok",
            "message": f"Slave '{node_id}' の登録情報を削除しました。",
            "removed": removed
        })
    async def handle_worker_agent_script(self, request: web.Request) -> web.Response:
        """Worker Agent スクリプト (agent.py) を配信 - Worker 側がインストール時に取得 (src.version.AGENT_VERSION と完全同期)"""
        agent_paths = [
            Path(__file__).resolve().parent / "static" / "agent.py",
            Path(__file__).resolve().parent.parent.parent / "docker" / "worker" / "agent" / "agent.py",
        ]
        for p in agent_paths:
            if p.exists():
                content = p.read_text(encoding="utf-8")
                # AGENT_VERSION を src.version.AGENT_VERSION (単一管理元) と確実に同期して配信
                content = re.sub(r'AGENT_VERSION\s*=\s*"[^"]*"', f'AGENT_VERSION = "{AGENT_VERSION}"', content)
                resp = web.Response(text=content, content_type="text/plain", charset="utf-8")
                resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                return resp
        return web.Response(text="# agent.py not found", status=404, content_type="text/plain", charset="utf-8")


    async def handle_login(self, request: web.Request) -> web.Response:
        if not self.auth.is_configured():
            return web.Response(
                text="<h2>Discord Client ID / Secret が未設定です。</h2>"
                     "<p>.env ファイルで <code>DISCORD_CLIENT_ID</code> および <code>DISCORD_CLIENT_SECRET</code> を設定してください。</p>",
                content_type="text/html",
                status=500
            )
        import secrets
        state = secrets.token_urlsafe(32)
        login_url = self.auth.get_login_url(state=state)
        response = web.HTTPFound(login_url)
        response.set_cookie(
            "oauth_state",
            state,
            max_age=600,
            httponly=True,
            samesite="Lax",
            secure=True
        )
        raise response

    async def handle_callback(self, request: web.Request) -> web.Response:
        code = request.query.get("code")
        error = request.query.get("error")
        received_state = request.query.get("state")
        expected_state = request.cookies.get("oauth_state")

        if error or not code:
            logger.warning(f"OAuth2 callback error or missing code: {error}")
            resp = web.HTTPFound("/?error=oauth_cancelled")
            resp.del_cookie("oauth_state")
            raise resp

        # CSRF state パラメータ検証
        import hmac
        if not expected_state or not received_state or not hmac.compare_digest(expected_state, received_state):
            logger.warning(
                f"OAuth2 CSRF state mismatch. Expected={bool(expected_state)}, Received={bool(received_state)}"
            )
            resp = web.HTTPFound("/?error=csrf_state_mismatch")
            resp.del_cookie("oauth_state")
            raise resp

        access_token = await self.auth.exchange_code(code)
        if not access_token:
            resp = web.Response(text="OAuth2トークンの取得に失敗しました。", status=400)
            resp.del_cookie("oauth_state")
            return resp

        user_info = await self.auth.fetch_user_info(access_token)
        if not user_info:
            resp = web.Response(text="ユーザー情報の取得に失敗しました。", status=400)
            resp.del_cookie("oauth_state")
            return resp

        user_id = int(user_info["id"])

        # サーバー参加判定
        is_member = await self.auth.is_user_in_bot_guilds(user_id)
        if not is_member:
            guild_names = [g.name for g in self.bot.guilds]
            guild_list_str = "、".join(guild_names) if guild_names else "なし"
            error_html = (
                "<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>"
                "<title>アクセス制限 - Distributed TTS</title>"
                "<style>body{background:#121316;color:#e1e2e6;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}"
                ".card{background:#1e1f22;padding:40px;border-radius:12px;max-width:500px;text-align:center;box-shadow:0 8px 24px rgba(0,0,0,0.4);border:1px solid #ff4757;}"
                "h2{color:#ff4757;margin-top:0;}p{line-height:1.6;color:#a0a4ab;}.btn{display:inline-block;margin-top:20px;padding:10px 24px;background:#5865F2;color:white;text-decoration:none;border-radius:8px;font-weight:bold;}"
                "</style></head><body><div class='card'>"
                "<h2>⚠️ アクセスできません</h2>"
                f"<p>アカウント <strong>{user_info.get('username')}</strong> は、このBotが導入されているDiscordサーバーに参加していません。</p>"
                f"<p style='font-size:13px;'>Bot導入中サーバー: <strong>{guild_list_str}</strong></p>"
                "<a href='/' class='btn'>トップへ戻る</a>"
                "</div></body></html>"
            )
            resp = web.Response(text=error_html, content_type="text/html", status=403)
            resp.del_cookie("oauth_state")
            return resp

        # 認証成功: セッションCookieを設定
        token = self.auth.create_session_token(user_info)
        response = web.HTTPFound("/")
        response.del_cookie("oauth_state")
        response.set_cookie(
            "ttsbot_session",
            token,
            max_age=86400 * 7,
            path="/",
            httponly=True,
            samesite="Lax",
            secure=True
        )
        return response

    async def handle_logout(self, request: web.Request) -> web.Response:
        response = web.HTTPFound("/")
        response.del_cookie("ttsbot_session")
        return response

    # --- API ハンドラ ---

    async def api_get_me(self, request: web.Request) -> web.Response:
        user = self._require_auth(request)
        user_id = user["user_id"]

        guild_id_str = request.query.get("guild_id")
        guild_id: Optional[int] = None
        if guild_id_str:
            try:
                guild_id = int(guild_id_str)
            except (ValueError, TypeError):
                guild_id = None

        available_speakers = list(self.config.voices.keys())
        settings = await self.db.get_or_create_user_settings(
            user_id=user_id,
            available_speaker_ids=available_speakers,
            default_pitch=self.config.default_pitch,
            default_speed=self.config.default_speed,
            strip_name_brackets=True,
            guild_id=guild_id
        )

        avatar_url = ""
        if user.get("avatar"):
            avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{user['avatar']}.png"

        return web.json_response({
            "user": {
                "id": str(user_id),
                "username": user["username"],
                "global_name": user.get("global_name", user["username"]),
                "avatar_url": avatar_url
            },
            "voice": settings,
            "current_guild_id": str(guild_id) if guild_id else None
        })

    async def api_update_voice(self, request: web.Request) -> web.Response:
        user = self._require_auth(request)
        data = await request.json()

        speaker_id = data.get("speaker_id")
        pitch = data.get("pitch")
        speed = data.get("speed")
        volume = data.get("volume")
        strip_name_brackets = data.get("strip_name_brackets")

        guild_id_raw = data.get("guild_id") or request.query.get("guild_id")
        guild_id: Optional[int] = None
        if guild_id_raw:
            try:
                guild_id = int(guild_id_raw)
            except (ValueError, TypeError):
                guild_id = None

        if not speaker_id or speaker_id not in self.config.voices:
            return web.json_response({"error": "Invalid speaker_id"}, status=400)

        try:
            speed_val = float(speed) if speed is not None else 1.0
            if not (0.5 <= speed_val <= 2.0):
                return web.json_response({"error": "Speed must be between 0.5 and 2.0"}, status=400)
        except ValueError:
            return web.json_response({"error": "Invalid speed format"}, status=400)

        volume_val: Optional[float] = None
        if volume is not None:
            try:
                v = float(volume)
                if v > 2.0:
                    v = v / 100.0  # 80 -> 0.8
                volume_val = max(0.0, min(v, 2.0))
            except (ValueError, TypeError):
                volume_val = 0.8

        strip_brackets_val = bool(strip_name_brackets) if strip_name_brackets is not None else None

        updated = await self.db.update_user_settings(
            user_id=user["user_id"],
            speaker_id=speaker_id,
            pitch=str(pitch) if pitch is not None else "0%",
            speed=speed_val,
            volume=volume_val,
            strip_name_brackets=strip_brackets_val,
            guild_id=guild_id
        )

        # ローカル音声モデルの場合はバックグラウンドで先行ウォームアップ（モデルロード）を実行
        if hasattr(self, "resource_manager") and self.resource_manager:
            safe_create_task(
                self.resource_manager.warmup_voice_by_id(speaker_id),
                name=f"warmup_{speaker_id}"
            )

        return web.json_response({"success": True, "voice": updated})

    async def api_get_user_guild_voices(self, request: web.Request) -> web.Response:
        """ユーザーが所属する各サーバーでの設定一覧（同期元セレクター用）"""
        user = self._require_auth(request)
        user_id = user["user_id"]

        saved_list = await self.db.get_all_guild_user_settings(user_id)
        saved_map = {row["guild_id"]: row for row in saved_list}

        result = []
        for g in self.bot.guilds:
            # ユーザーが所属しているかチェック
            m = await self._get_or_fetch_member(g, user_id)
            if not m:
                continue

            voice_data = saved_map.get(g.id)
            speaker_id = voice_data["speaker_id"] if voice_data else ""
            v_cfg = self.config.voices.get(speaker_id) if speaker_id else None
            speaker_name = v_cfg.name if v_cfg else (speaker_id or "未設定 (初期値)")

            result.append({
                "guild_id": str(g.id),
                "guild_name": g.name,
                "guild_icon": g.icon.url if g.icon else None,
                "has_custom_setting": voice_data is not None,
                "speaker_id": speaker_id,
                "speaker_name": speaker_name,
                "pitch": voice_data["pitch"] if voice_data else "0%",
                "speed": voice_data["speed"] if voice_data else 1.0,
                "strip_name_brackets": voice_data.get("strip_name_brackets", True) if voice_data else True,
                "updated_at": voice_data["updated_at"] if voice_data else None
            })

        return web.json_response({"guilds": result})

    async def api_sync_voice(self, request: web.Request) -> web.Response:
        """指定サーバー (from_guild_id) の音声設定を現在のサーバー (to_guild_id) に同期"""
        user = self._require_auth(request)
        user_id = user["user_id"]
        data = await request.json()

        from_guild_id = data.get("from_guild_id")
        to_guild_id = data.get("to_guild_id")

        if not from_guild_id or not to_guild_id:
            return web.json_response({"error": "from_guild_id and to_guild_id are required"}, status=400)

        try:
            from_id = int(from_guild_id)
            to_id = int(to_guild_id)
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid guild IDs"}, status=400)

        updated = await self.db.sync_guild_user_settings(
            user_id=user_id,
            from_guild_id=from_id,
            to_guild_id=to_id
        )

        if not updated:
            return web.json_response({"error": "Source guild voice settings not found"}, status=404)

        return web.json_response({"success": True, "voice": updated})

    async def api_preview_audio(self, request: web.Request) -> web.Response:
        """ブラウザ上で音声をテスト試聴するためのエンドポイント"""
        self._require_auth(request)
        data = await request.json()

        text = data.get("text", "こんにちは！これはテスト読み上げです。").strip()
        speaker_id = data.get("speaker_id", self.config.default_speaker)
        pitch = data.get("pitch", "0%")
        try:
            speed = float(data.get("speed", 1.0))
        except ValueError:
            speed = 1.0

        if not text:
            return web.json_response({"error": "Text is empty"}, status=400)

        try:
            # プレビュー用音声の取得 (キャッシュ機構を利用)
            audio_path = await self.tts_manager.get_audio_path(
                user_id=0,  # プレビュー共有用ID
                speaker_id=speaker_id,
                pitch=str(pitch),
                speed=speed,
                text=text
            )
            with open(audio_path, "rb") as f:
                content = f.read()

            return web.Response(body=content, content_type="audio/wav")
        except Exception as e:
            logger.error(f"Error generating preview audio: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def api_get_voices(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        voices_list = []
        for v_id, v in self.config.voices.items():
            voices_list.append({
                "id": v.id,
                "name": v.name,
                "gender": v.gender,
                "description": v.description,
                "provider": v.provider,
                "azure_voice": v.azure_voice,
                "google_voice": v.google_voice,
                "style_id": v.style_id
            })
        return web.json_response({"voices": voices_list})

    # --- ギルド切り替え & ロール関連 API ---

    async def api_get_guilds(self, request: web.Request) -> web.Response:
        """ログインユーザーが所属し、Botも参加しているギルド一覧と権限を返却"""
        user = self._require_auth(request)
        user_id = user["user_id"]

        result = []
        for guild in self.bot.guilds:
            member = await self._get_or_fetch_member(guild, user_id)
            if not member:
                continue

            is_admin = member.guild_permissions.administrator or (guild.owner_id == user_id)
            guild_settings = await self.db.get_guild_settings(guild.id, self.config)
            admin_role_id = guild_settings.get("admin_role_id", 0)

            has_role = is_admin
            if admin_role_id and admin_role_id > 0:
                if any(r.id == admin_role_id for r in member.roles):
                    has_role = True

            icon_url = str(guild.icon.url) if guild.icon else None
            vc = guild.voice_client

            result.append({
                "id": str(guild.id),
                "name": guild.name,
                "icon_url": icon_url,
                "is_admin": is_admin,
                "has_role": has_role,
                "member_count": guild.member_count,
                "connected_vc": vc.channel.name if (vc and vc.is_connected() and vc.channel) else None
            })

        return web.json_response({"guilds": result})

    async def api_get_guild_roles(self, request: web.Request) -> web.Response:
        """指定ギルドのロール一覧を返却（管理者ロール設定用）"""
        guild_id_str = request.match_info.get("guild_id")
        guild, perms = await self._get_guild_and_perms(request, guild_id_str)

        roles_list = []
        for r in sorted(guild.roles, key=lambda x: x.position, reverse=True):
            if r.name == "@everyone":
                continue
            color_hex = f"#{r.color.value:06x}" if r.color.value else "#99aab5"
            roles_list.append({
                "id": str(r.id),
                "name": r.name,
                "color": color_hex,
                "position": r.position
            })

        return web.json_response({"roles": roles_list, "guild_id": str(guild.id)})

    async def api_get_guild_channels(self, request: web.Request) -> web.Response:
        """指定ギルドのボイスチャンネル・テキストチャンネル一覧を返却"""
        guild_id_str = request.match_info.get("guild_id")
        guild, perms = await self._get_guild_and_perms(request, guild_id_str)

        voice_channels = []
        text_channels = []

        for ch in sorted(guild.channels, key=lambda x: x.position):
            if isinstance(ch, discord.VoiceChannel):
                voice_channels.append({
                    "id": str(ch.id),
                    "name": ch.name,
                    "position": ch.position
                })
            elif isinstance(ch, discord.TextChannel):
                text_channels.append({
                    "id": str(ch.id),
                    "name": ch.name,
                    "position": ch.position
                })

        return web.json_response({
            "guild_id": str(guild.id),
            "voice_channels": voice_channels,
            "text_channels": text_channels
        })

    # --- 単語辞書 API ---

    async def api_get_dict(self, request: web.Request) -> web.Response:
        guild, _ = await self._get_guild_and_perms(request, request.query.get("guild_id"))
        words = await self.db.get_dictionary_words(guild.id)
        return web.json_response({
            "guild_id": str(guild.id),
            "dictionary": [{"word": w, "reading": r} for w, r in words]
        })

    async def api_add_dict(self, request: web.Request) -> web.Response:
        data = await request.json()
        guild, perms = await self._get_guild_and_perms(request, data.get("guild_id"))

        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({
                "error": "Access denied. Administrator or assigned role required to add dictionary words."
            }, status=403)

        word = data.get("word", "").strip()
        reading = data.get("reading", "").strip()

        if not word or not reading:
            return web.json_response({"error": "Word and reading are required"}, status=400)

        await self.db.add_dictionary_word(guild.id, word, reading)
        purged = await self.cache_manager.purge_by_word(word)
        return web.json_response({"success": True, "purged_cache_count": purged})

    async def api_delete_dict(self, request: web.Request) -> web.Response:
        word = request.match_info.get("word", "").strip()
        guild, perms = await self._get_guild_and_perms(request, request.query.get("guild_id"))

        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({
                "error": "Access denied. Administrator or assigned role required to delete dictionary words."
            }, status=403)

        if not word:
            return web.json_response({"error": "Word is required"}, status=400)

        deleted = await self.db.delete_dictionary_word(guild.id, word)
        if deleted:
            purged = await self.cache_manager.purge_by_word(word)
            return web.json_response({"success": True, "purged_cache_count": purged})
        return web.json_response({"error": "Word not found"}, status=404)

    async def api_update_dict(self, request: web.Request) -> web.Response:
        data = await request.json()
        guild, perms = await self._get_guild_and_perms(request, data.get("guild_id"))

        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({
                "error": "Access denied. Administrator or assigned role required to update dictionary words."
            }, status=403)

        old_word = data.get("old_word", "").strip()
        new_word = data.get("new_word", "").strip()
        reading = data.get("reading", "").strip()

        if not old_word or not new_word or not reading:
            return web.json_response({"error": "old_word, new_word, and reading are required"}, status=400)

        updated = await self.db.update_dictionary_word(guild.id, old_word, new_word, reading)
        if updated:
            purged = await self.cache_manager.purge_by_words([old_word, new_word])
            return web.json_response({"success": True, "purged_cache_count": purged})
        return web.json_response({"error": "Word not found"}, status=404)

    # --- NGワード API ---

    async def api_get_ng(self, request: web.Request) -> web.Response:
        guild, _ = await self._get_guild_and_perms(request, request.query.get("guild_id"))
        words = await self.db.get_ng_words(guild.id)
        return web.json_response({
            "guild_id": str(guild.id),
            "ng_words": words
        })

    async def api_add_ng(self, request: web.Request) -> web.Response:
        data = await request.json()
        guild, perms = await self._get_guild_and_perms(request, data.get("guild_id"))

        word = data.get("word", "").strip()
        if not word:
            return web.json_response({"error": "Word is required"}, status=400)

        await self.db.add_ng_word(guild.id, word)
        purged = await self.cache_manager.purge_by_word(word)
        return web.json_response({"success": True, "purged_cache_count": purged})

    async def api_delete_ng(self, request: web.Request) -> web.Response:
        word = request.match_info.get("word", "").strip()
        guild, perms = await self._get_guild_and_perms(request, request.query.get("guild_id"))

        if not word:
            return web.json_response({"error": "Word is required"}, status=400)

        deleted = await self.db.delete_ng_word(guild.id, word)
        if deleted:
            purged = await self.cache_manager.purge_by_word(word)
            return web.json_response({"success": True, "purged_cache_count": purged})
        return web.json_response({"error": "Word not found"}, status=404)

    async def api_import_ng_preset(self, request: web.Request) -> web.Response:
        data = await request.json()
        guild, perms = await self._get_guild_and_perms(request, data.get("guild_id"))

        count = await self.db.import_default_ng_words(guild.id)
        return web.json_response({"success": True, "imported_count": count})

    async def api_get_ai_suggestions(self, request: web.Request) -> web.Response:
        """AI提案単語リストを取得 (認証必須)"""
        user = self._require_auth(request)
        guild, _ = await self._get_guild_and_perms(request, request.query.get("guild_id"))
        status = request.query.get("status", "pending")
        suggestions = await self.db.get_ai_suggestions(guild.id, status=status)
        return web.json_response({
            "guild_id": str(guild.id),
            "suggestions": suggestions
        })

    async def api_approve_ai_suggestion(self, request: web.Request) -> web.Response:
        """AI提案を承認して本辞書へ登録 (管理者または担当ロールのみ)"""
        user = self._require_auth(request)
        try:
            s_id = int(request.match_info.get("id", 0))
        except ValueError:
            return web.json_response({"error": "Invalid suggestion ID"}, status=400)

        item = await self.db.get_ai_suggestion(s_id)
        if not item:
            return web.json_response({"error": "Suggestion not found"}, status=404)

        guild, perms = await self._get_guild_and_perms(request, str(item["guild_id"]))
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({
                "error": "Access denied. Administrator or assigned role required to approve AI suggestions."
            }, status=403)

        approved = await self.db.approve_ai_suggestion(s_id)
        if approved:
            await self.cache_manager.purge_by_word(approved["word"])
            return web.json_response({"success": True, "approved": approved})
        return web.json_response({"error": "Suggestion not found"}, status=404)

    async def api_reject_ai_suggestion(self, request: web.Request) -> web.Response:
        """AI提案を却下 (管理者または担当ロールのみ)"""
        user = self._require_auth(request)
        try:
            s_id = int(request.match_info.get("id", 0))
        except ValueError:
            return web.json_response({"error": "Invalid suggestion ID"}, status=400)

        item = await self.db.get_ai_suggestion(s_id)
        if not item:
            return web.json_response({"error": "Suggestion not found"}, status=404)

        guild, perms = await self._get_guild_and_perms(request, str(item["guild_id"]))
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({
                "error": "Access denied. Administrator or assigned role required to reject AI suggestions."
            }, status=403)

        rejected = await self.db.reject_ai_suggestion(s_id)
        return web.json_response({"success": rejected})

    async def api_delete_ai_suggestion(self, request: web.Request) -> web.Response:
        """AI提案を物理削除 (管理者または担当ロールのみ)"""
        user = self._require_auth(request)
        try:
            s_id = int(request.match_info.get("id", 0))
        except ValueError:
            return web.json_response({"error": "Invalid suggestion ID"}, status=400)

        item = await self.db.get_ai_suggestion(s_id)
        if not item:
            return web.json_response({"error": "Suggestion not found"}, status=404)

        guild, perms = await self._get_guild_and_perms(request, str(item["guild_id"]))
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({
                "error": "Access denied. Administrator or assigned role required to delete AI suggestions."
            }, status=403)

        deleted = await self.db.delete_ai_suggestion(s_id)
        return web.json_response({"success": deleted})

    async def api_import_users(self, request: web.Request) -> web.Response:
        """ユーザー名（サーバー表示名）を単語辞書にインポート"""
        user = self._require_auth(request)
        data = await request.json()
        guild, perms = await self._get_guild_and_perms(request, data.get("guild_id"))

        scope = data.get("scope", "me")  # "me" または "all"
        if scope == "all" and not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({
                "error": "Access denied. Administrator or assigned role required to import all users."
            }, status=403)

        names_to_import: List[str] = []

        if scope == "me":
            member = await self._get_or_fetch_member(guild, user["user_id"])
            display_name = member.display_name if member else user["username"]
            names_to_import.append(display_name)
        elif scope == "all":
            # ギルド内のBot以外のメンバー全員の表示名
            for m in guild.members:
                if not m.bot:
                    names_to_import.append(m.display_name)
        else:
            return web.json_response({"error": "Invalid scope. Use 'me' or 'all'."}, status=400)

        imported_count = await self.db.import_user_names(guild.id, names_to_import)
        purged = await self.cache_manager.purge_by_words(names_to_import)

        return web.json_response({
            "success": True,
            "scope": scope,
            "imported_count": imported_count,
            "purged_cache_count": purged
        })

    async def api_import_dict_batch(self, request: web.Request) -> web.Response:
        """単語辞書テキスト一括インポート (単語:読み方リスト)"""
        user = self._require_auth(request)
        data = await request.json()
        guild, perms = await self._get_guild_and_perms(request, data.get("guild_id"))

        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({
                "error": "Access denied. Administrator or assigned role required to batch import dictionary words."
            }, status=403)

        items = data.get("items", [])
        if not isinstance(items, list):
            return web.json_response({"error": "Items must be a list"}, status=400)

        if len(items) > 1000:
            return web.json_response({"error": "一度にインポートできる単語は最大1,000件です"}, status=400)

        cleaned_items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            w = str(item.get("word", "")).strip()
            r = str(item.get("reading", "")).strip()
            if w and r:
                cleaned_items.append((w, r))

        if not cleaned_items:
            return web.json_response({"error": "登録可能な単語が見つかりませんでした"}, status=400)

        imported_count = await self.db.add_dictionary_words_batch(guild.id, cleaned_items)
        purged = await self.cache_manager.purge_by_words([w for w, _ in cleaned_items])

        return web.json_response({
            "success": True,
            "imported_count": imported_count,
            "purged_cache_count": purged
        })

    # --- ボット設定 API (ギルド個別設定 & 権限制御) ---

    async def api_get_config(self, request: web.Request) -> web.Response:
        guild, perms = await self._get_guild_and_perms(request, request.query.get("guild_id"))

        # 設定されたロール + 管理者のみ閲覧可能
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response(
                {"error": "Access denied. Only administrators and assigned roles can view bot settings."},
                status=403
            )

        settings = await self.db.get_guild_settings(guild.id, self.config)
        raw_pairs = settings.get("auto_join_pairs", [])
        str_pairs = [{"vc_id": str(p["vc_id"]), "text_id": str(p["text_id"])} for p in raw_pairs]

        # 稼働中のBot数を動的に取得 (最低1台、最大10台)
        active_bots = 1
        if self.bot_pool and hasattr(self.bot_pool, "get_active_bot_count"):
            active_bots = self.bot_pool.get_active_bot_count()
        elif self.bot_pool and hasattr(self.bot_pool, "bots"):
            ready_c = sum(1 for b in self.bot_pool.bots.values() if b.is_ready())
            active_bots = max(1, min(10, ready_c if ready_c > 0 else len(self.bot_pool.bots)))

        return web.json_response({
            "guild_id": str(guild.id),
            "command_prefix": settings.get("command_prefix", "!"),
            "max_text_length": settings.get("max_text_length", 50),
            "dedup_window_seconds": settings.get("dedup_window_seconds", 5.0),
            "dedup_max_length": settings.get("dedup_max_length", 10),
            "auto_join_vc_id": str(settings.get("auto_join_vc_id", 0)),
            "auto_join_text_channel_id": str(settings.get("auto_join_text_channel_id", 0)),
            "auto_join_pairs": str_pairs,
            "max_auto_join_pairs": active_bots,
            "active_bot_count": active_bots,
            "admin_role_id": str(settings.get("admin_role_id", 0)),
            "default_speaker": settings.get("default_speaker", self.config.default_speaker),
            "read_username": bool(settings.get("read_username", 0)),
            "announce_vc_events": bool(settings.get("announce_vc_events", 0)),
            "speculative_timeout_seconds": float(settings.get("speculative_timeout_seconds", 1.5) or 1.5),
            "ignore_prefixes": settings.get("ignore_prefixes", [";", "；"]),
            "disaster_alert_enabled": bool(settings.get("disaster_alert_enabled", 1)),
            "disaster_min_scale": int(settings.get("disaster_min_scale", 30)),
            "disaster_pref_filter": str(settings.get("disaster_pref_filter", "")),
            "disaster_alert_weather": bool(settings.get("disaster_alert_weather", 1)),
            "disaster_volume": float(settings.get("disaster_volume", 1.0) if settings.get("disaster_volume") is not None else 1.0),
            "error_contact_user_id": str(settings.get("error_contact_user_id", "") or ""),
            "is_admin": perms["is_admin"],
            "has_role": perms["has_role"]
        })

    async def api_update_config(self, request: web.Request) -> web.Response:
        data = await request.json()
        guild, perms = await self._get_guild_and_perms(request, data.get("guild_id"))

        # 設定されたロール + 管理者のみ保存可能
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response(
                {"error": "Access denied. Only administrators and assigned roles can update bot settings."},
                status=403
            )

        update_fields = {}
        if "command_prefix" in data:
            update_fields["command_prefix"] = str(data["command_prefix"])
        if "max_text_length" in data:
            update_fields["max_text_length"] = int(data["max_text_length"])
        if "dedup_window_seconds" in data:
            update_fields["dedup_window_seconds"] = float(data["dedup_window_seconds"])
        if "dedup_max_length" in data:
            update_fields["dedup_max_length"] = int(data["dedup_max_length"])
        if "auto_join_vc_id" in data:
            update_fields["auto_join_vc_id"] = int(data["auto_join_vc_id"] or 0)
        if "auto_join_text_channel_id" in data:
            update_fields["auto_join_text_channel_id"] = int(data["auto_join_text_channel_id"] or 0)
        if "auto_join_pairs" in data:
            raw_pairs = data["auto_join_pairs"]
            valid_pairs = []
            max_limit = 10
            if self.bot_pool and hasattr(self.bot_pool, "get_active_bot_count"):
                max_limit = self.bot_pool.get_active_bot_count()
            slice_limit = max(1, min(10, max_limit))
            if isinstance(raw_pairs, list):
                for p in raw_pairs[:slice_limit]:
                    try:
                        vc_id = int(p.get("vc_id", 0))
                        text_id = int(p.get("text_id", 0))
                        if vc_id > 0 and text_id > 0:
                            valid_pairs.append({"vc_id": vc_id, "text_id": text_id})
                    except (ValueError, TypeError):
                        pass
            update_fields["auto_join_pairs"] = valid_pairs
        if "default_speaker" in data and data["default_speaker"] in self.config.voices:
            update_fields["default_speaker"] = str(data["default_speaker"])
        if "read_username" in data:
            update_fields["read_username"] = 1 if data["read_username"] else 0
        if "announce_vc_events" in data:
            update_fields["announce_vc_events"] = 1 if data["announce_vc_events"] else 0
        if "speculative_timeout_seconds" in data:
            try:
                val = float(data["speculative_timeout_seconds"])
                update_fields["speculative_timeout_seconds"] = max(0.2, min(5.0, val))
                self.config.speculative_timeout_seconds = update_fields["speculative_timeout_seconds"]
            except (ValueError, TypeError):
                pass
        if "ignore_prefixes" in data:
            update_fields["ignore_prefixes"] = data["ignore_prefixes"]
        if "disaster_alert_enabled" in data:
            update_fields["disaster_alert_enabled"] = 1 if data["disaster_alert_enabled"] else 0
        if "disaster_min_scale" in data:
            try:
                update_fields["disaster_min_scale"] = int(data["disaster_min_scale"])
            except (ValueError, TypeError):
                pass
        if "disaster_pref_filter" in data:
            update_fields["disaster_pref_filter"] = str(data["disaster_pref_filter"] or "")
        if "disaster_alert_weather" in data:
            update_fields["disaster_alert_weather"] = 1 if data["disaster_alert_weather"] else 0
        if "disaster_volume" in data:
            try:
                v = float(data["disaster_volume"])
                update_fields["disaster_volume"] = max(0.0, min(v, 2.0))
            except (ValueError, TypeError):
                pass
        if "error_contact_user_id" in data:
            try:
                raw_cid = str(data["error_contact_user_id"]).strip()
                update_fields["error_contact_user_id"] = int(raw_cid) if raw_cid.isdigit() else 0
            except (ValueError, TypeError):
                pass

        # ロール設定は管理者のみ可能
        if "admin_role_id" in data:
            if not perms["is_admin"]:
                return web.json_response(
                    {"error": "Access denied. Only guild administrators can configure the admin role."},
                    status=403
                )
            update_fields["admin_role_id"] = int(data["admin_role_id"] or 0)

        updated = await self.db.update_guild_settings(guild.id, **update_fields)
        return web.json_response({"success": True, "settings": updated})

    async def api_reload_config(self, request: web.Request) -> web.Response:
        """設定ファイル (config.yaml) および環境変数 (.env) をホットリロード"""
        await self._require_admin(request)

        try:
            from src.config import reload_config
            reload_config(self.config)

            # DBキャッシュをクリア
            if hasattr(self.db, "invalidate_guild_cache"):
                self.db.invalidate_guild_cache()
            if hasattr(self.db, "invalidate_user_cache"):
                self.db.invalidate_user_cache()

            # TTS Managerのノード再設定などがあれば反映
            if hasattr(self.tts_manager, "reload_nodes"):
                self.tts_manager.reload_nodes()

            logger.info("Configuration reloaded successfully via Web API.")
            return web.json_response({
                "success": True,
                "message": "Configuration (config.yaml and .env) reloaded successfully."
            })
        except Exception as e:
            logger.error(f"Failed to reload config: {e}")
            return web.json_response({"error": f"Failed to reload configuration: {e}"}, status=500)

    # --- システム & キャッシュ状況 API (権限制御 & TTS利用量グラフ) ---

    async def api_get_stats(self, request: web.Request) -> web.Response:
        guild, perms = await self._get_guild_and_perms(request, request.query.get("guild_id"))

        # 設定されたロール + 管理者のみ閲覧可能
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response(
                {"error": "Access denied. Only administrators and assigned roles can view system stats."},
                status=403
            )

        # 直近2秒以内のキャッシュがあれば返却（高頻度ポーリングによる負荷軽減）
        now = time.time()
        if self._cached_stats_data and (now - self._cached_stats_time) < 2.0:
            cached_resp = dict(self._cached_stats_data)
            cached_resp["permissions"] = perms
            return web.json_response(cached_resp, headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            })

        # 1. 音声キャッシュ状況 (Cloud 10GB と VOICEVOX 20GB を分離取得)
        stats = self.cache_manager.get_stats()
        cloud_s = stats["cloud"]
        vv_s = stats["voicevox"]

        # 2. サーバー接続状況
        guilds_data = []
        for g in self.bot.guilds:
            vc = g.voice_client
            guilds_data.append({
                "id": str(g.id),
                "name": g.name,
                "member_count": g.member_count,
                "connected_vc": vc.channel.name if (vc and vc.is_connected() and vc.channel) else None
            })

        # 3. TTS利用状況 (Azure 50万文字/月, Google 100万文字/月, VOICEVOX)
        tts_usage = await self.db.get_monthly_tts_usage()

        data_to_cache = {
            "cache": {
                "used_bytes": cloud_s["size_bytes"],
                "used_mb": round(cloud_s["size_bytes"] / (1024 * 1024), 2),
                "used_gb": round(cloud_s["size_bytes"] / (1024 * 1024 * 1024), 3),
                "max_bytes": cloud_s["max_bytes"],
                "max_gb": round(cloud_s["max_bytes"] / (1024 * 1024 * 1024), 1),
                "target_bytes": self.config.cache_target_size_bytes,
                "file_count": cloud_s["count"]
            },
            "cache_voicevox": {
                "used_bytes": vv_s["size_bytes"],
                "used_mb": round(vv_s["size_bytes"] / (1024 * 1024), 2),
                "used_gb": round(vv_s["size_bytes"] / (1024 * 1024 * 1024), 3),
                "max_bytes": vv_s["max_bytes"],
                "max_gb": round(vv_s["max_bytes"] / (1024 * 1024 * 1024), 1),
                "target_bytes": self.config.voicevox_cache_target_size_bytes,
                "file_count": vv_s["count"]
            },
            "bot": {
                "guilds": guilds_data,
                "total_guilds": len(self.bot.guilds)
            },
            "bot_pool": self.bot_pool.get_pool_status() if self.bot_pool else [],
            "nodes": self.tts_manager.get_node_status() if hasattr(self.tts_manager, "get_node_status") else {},
            "tts_usage": tts_usage,
            "latest_agent_version": AGENT_VERSION
        }
        self._cached_stats_data = data_to_cache
        self._cached_stats_time = now

        resp_with_perms = dict(data_to_cache)
        resp_with_perms["permissions"] = perms
        return web.json_response(resp_with_perms, headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        })

    async def api_get_system_metrics(self, request: web.Request) -> web.Response:
        """ホストおよび各コンテナのCPU/メモリメトリクス推移（30秒間隔）と並列制御ステータスを取得"""
        guild, perms = await self._get_guild_and_perms(request, request.query.get("guild_id"))
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response(
                {"error": "Access denied. Only administrators and assigned roles can view system metrics."},
                status=403
            )

        rm = getattr(self.tts_manager, "resource_manager", None)
        if rm:
            metrics_data = rm.get_metrics_data()
        else:
            metrics_data = {
                "history": [],
                "status": {
                    "mode": "standalone",
                    "host_cpu": 0.0,
                    "active_synthesis": 0,
                    "engine_active": {},
                    "history_points": 0,
                }
            }
        try:
            metrics_data["latency"] = latency_tracker.get_summary()
        except Exception as e:
            logger.debug(f"Failed to get latency summary: {e}")
            metrics_data["latency"] = {}

        try:
            from utils.latency_profiler import latency_profiler
            metrics_data["profiler"] = latency_profiler.get_all_stats()
        except Exception as e:
            logger.debug(f"Failed to get profiler stats: {e}")
            metrics_data["profiler"] = {}

        # Workerノード別時系列メトリクス・最新リソース
        metrics_data["workers_history"] = {
            k: list(v) for k, v in self.workers_metrics_history.items()
        }
        metrics_data["workers_current"] = self.workers_latest_resources

        return web.json_response(metrics_data)

    async def api_get_profiler_stats(self, request: web.Request) -> web.Response:
        """ローカルエンジン推論速度統計プロファイラ情報（ms/文字、最強GPU判定等）を取得"""
        guild, perms = await self._get_guild_and_perms(request, request.query.get("guild_id"))
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({"error": "Access denied."}, status=403)
        try:
            from utils.latency_profiler import latency_profiler
            stats = latency_profiler.get_all_stats()
            return web.json_response({
                "success": True,
                "profiler": stats
            })
        except Exception as e:
            logger.error(f"Failed to get profiler stats: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def api_test_simulate(self, request: web.Request) -> web.Response:
        """10名30発言の連続会話シミュレーションテストを実行（Discord検知フルパイプライン、管理者のみ）"""
        await self._require_admin(request)

        try:
            body = await request.json()
        except Exception:
            body = {}

        guild_id = body.get("guild_id")
        if not guild_id and self.bot.guilds:
            guild_id = self.bot.guilds[0].id

        vc_cog = self.bot.get_cog("VCHandlerCog")
        if not vc_cog:
            return web.json_response({"error": "VCHandlerCog not loaded"}, status=500)

        guild = self.bot.get_guild(guild_id) if guild_id else None
        messages = body.get("messages", [])
        if not messages:
            return web.json_response({"error": "No messages provided"}, status=400)

        import wave
        import io
        from src.cogs.vc_handler import QueueItem
        from src.utils.text_cleaner import TextCleaner

        mode = body.get("mode", "streaming")
        test_queue = asyncio.Queue()
        results = []

        async def _test_player_worker():
            while True:
                item = await test_queue.get()
                if item is None:
                    test_queue.task_done()
                    break

                t_dequeued = time.time()
                try:
                    voice_cfg = self.config.voices.get(item.speaker_id)
                    spk_name = voice_cfg.name if voice_cfg else item.speaker_id

                    if mode == "streaming":
                        # 超低遅延ストリーミング・パイプライン方式
                        all_chunks = TextCleaner.split_into_chunks(item.text, author_prefix=item.author_prefix)
                        if not all_chunks:
                            test_queue.task_done()
                            continue

                        chunks_meta = []
                        total_audio_dur = 0.0
                        min_buffer_remaining_ms = float('inf')

                        # 第1チャンク（名前プレフィックスまたは第1文）
                        first_chunk = all_chunks[0]
                        c0_s = time.time()
                        c0_bytes, c0_prov, c0_worker, c0_cached = await self.tts_manager.get_chunk_audio(
                            text=first_chunk.text,
                            speaker_id=item.speaker_id,
                            pitch=item.pitch,
                            speed=item.speed,
                            user_id=item.user_id
                        )
                        c0_e = time.time()
                        dur0 = len(c0_bytes) / 48000.0 if c0_bytes else 1.0
                        if c0_bytes and len(c0_bytes) > 44:
                            try:
                                with wave.open(io.BytesIO(c0_bytes), "rb") as wf:
                                    dur0 = wf.getnframes() / float(wf.getframerate())
                            except Exception:
                                pass
                        total_audio_dur += dur0

                        # 第1チャンク完了で即座に再生開始！ (TTFS計測)
                        t_play_start = time.time()
                        c0_synth_dur = round((c0_e - c0_s) * 1000, 1)
                        c0_audio_dur = round(dur0 * 1000, 1)
                        chunks_meta.append({
                            "chunk_index": 0,
                            "text": first_chunk.text if not getattr(first_chunk, "is_beep", False) else "🚫 [自主規制ピー音]",
                            "char_count": len(first_chunk.text) if not getattr(first_chunk, "is_beep", False) else 3,
                            "role": "name" if getattr(first_chunk, "is_name_prefix", False) else ("beep" if getattr(first_chunk, "is_beep", False) else "body"),
                            "provider": c0_prov,
                            "worker": c0_worker,
                            "is_cached": c0_cached,
                            "is_fallback": False,
                            "synth_ms": c0_synth_dur,
                            "synthesis_ms": c0_synth_dur,
                            "duration_ms": c0_audio_dur,
                            "t_synth_start": c0_s,
                            "t_synth_end": c0_e,
                            "buffer_remaining_ms": c0_audio_dur,
                            "buffer_headroom_ms": c0_audio_dur,
                        })

                        # 残りチャンクの並行合成
                        if len(all_chunks) > 1:
                            rem = all_chunks[1:]
                            async def _synth_c(ch):
                                ts = time.time()
                                b, p, w, c = await self.tts_manager.get_chunk_audio(
                                    text=ch.text,
                                    speaker_id=item.speaker_id,
                                    pitch=item.pitch,
                                    speed=item.speed,
                                    user_id=item.user_id
                                )
                                te = time.time()
                                return ch, b, p, w, c, ts, te

                            rem_res = await asyncio.gather(*[_synth_c(ch) for ch in rem])
                            for ch, b, p, w, c, ts, te in rem_res:
                                elapsed = time.time() - t_play_start
                                buffer_remaining_s = total_audio_dur - elapsed
                                buffer_remaining_ms = buffer_remaining_s * 1000
                                min_buffer_remaining_ms = min(min_buffer_remaining_ms, buffer_remaining_ms)
                                dur_c = 1.0
                                if b and len(b) > 44:
                                    try:
                                        with wave.open(io.BytesIO(b), "rb") as wf:
                                            dur_c = wf.getnframes() / float(wf.getframerate())
                                    except Exception:
                                        pass
                                total_audio_dur += dur_c
                                rem_synth_dur = round((te - ts) * 1000, 1)
                                rem_audio_dur = round(dur_c * 1000, 1)
                                buf_rem_rounded = round(buffer_remaining_ms, 1)
                                chunks_meta.append({
                                    "chunk_index": len(chunks_meta),
                                    "text": ch.text if not getattr(ch, "is_beep", False) else "🚫 [自主規制ピー音]",
                                    "char_count": len(ch.text) if not getattr(ch, "is_beep", False) else 3,
                                    "role": "beep" if getattr(ch, "is_beep", False) else "body",
                                    "provider": p,
                                    "worker": w,
                                    "is_cached": c,
                                    "is_fallback": False,
                                    "synth_ms": rem_synth_dur,
                                    "synthesis_ms": rem_synth_dur,
                                    "duration_ms": rem_audio_dur,
                                    "t_synth_start": ts,
                                    "t_synth_end": te,
                                    "buffer_remaining_ms": buf_rem_rounded,
                                    "buffer_headroom_ms": buf_rem_rounded,
                                })

                        total_synth_ms = sum(c["synth_ms"] for c in chunks_meta)
                        if t_play_start > 0 and len(chunks_meta) > 1:
                            masked_ms = 0.0
                            playback_cursor_ms = 0.0
                            for ci, ch_m in enumerate(chunks_meta):
                                if ci == 0:
                                    playback_cursor_ms += ch_m["duration_ms"]
                                    continue
                                t_se = ch_m.get("t_synth_end")
                                if t_se:
                                    synth_done_offset_ms = (t_se - t_play_start) * 1000
                                    if synth_done_offset_ms <= playback_cursor_ms:
                                        masked_ms += ch_m["synth_ms"]
                                playback_cursor_ms += ch_m["duration_ms"]
                            post_c0_synth = total_synth_ms - (chunks_meta[0]["synth_ms"] if chunks_meta else 0)
                            masking_ratio = round((masked_ms / post_c0_synth * 100), 1) if post_c0_synth > 0 else 100.0
                        else:
                            masking_ratio = 100.0 if total_synth_ms > 0 else 0.0

                        t_recv = getattr(item, "received_at", t_dequeued)
                        turn_ttfs = max(0.0, (t_play_start - t_dequeued) * 1000.0)
                        e2e_ttfs = max(0.0, (t_play_start - t_recv) * 1000.0)
                        core_ttfs = max(0.0, (c0_e - t_dequeued) * 1000.0)
                        split_ms = getattr(all_chunks[0], "split_ms", 0.0) if all_chunks else 0.0
                        calc_headroom = round(min_buffer_remaining_ms if min_buffer_remaining_ms != float('inf') else 0.0, 1)
                        gap_lag = round(abs(min(0.0, min_buffer_remaining_ms)) if min_buffer_remaining_ms != float('inf') else 0.0, 1)

                        rec = latency_tracker.record(
                            user_name=item.user_name,
                            text=item.text,
                            speaker_name=spk_name,
                            provider=chunks_meta[0]["provider"],
                            worker_label=chunks_meta[0]["worker"],
                            t_discord_created=getattr(item, "discord_created_at", 0.0),
                            t_received=t_recv,
                            t_queued=getattr(item, "queued_at", 0.0),
                            t_dequeued=t_dequeued,
                            t_synth_start=c0_s,
                            t_synth_end=c0_e,
                            t_play_start=t_play_start,
                            is_cached=chunks_meta[0]["is_cached"],
                            is_fallback=False,
                            chunks=chunks_meta,
                            ttfs_ms=round(turn_ttfs, 1),
                            turn_ttfs_ms=round(turn_ttfs, 1),
                            e2e_ttfs_ms=round(e2e_ttfs, 1),
                            underrun_ms=0.0,
                            split_ms=round(split_ms, 1),
                            buffer_headroom_ms=calc_headroom,
                            gap_lag_ms=gap_lag,
                            masking_ratio=masking_ratio,
                            total_audio_duration_ms=round(total_audio_dur * 1000.0, 1),
                            preroll_wait_ms=0.0,
                            core_ttfs_ms=round(core_ttfs, 1),
                        )
                        results.append(asdict(rec))
                        play_wait = min(total_audio_dur, 2.0)
                        await asyncio.sleep(play_wait)

                    else:
                        # 従来のバッチ一括方式
                        if item.synth_task:
                            audio_path, t_synth_start, t_synth_end, meta = await item.synth_task
                        else:
                            t_synth_start = time.time()
                            audio_path = await self.tts_manager.get_audio_path(
                                user_id=item.user_id,
                                speaker_id=item.speaker_id,
                                pitch=item.pitch,
                                speed=item.speed,
                                text=item.text,
                                author_prefix=item.author_prefix
                            )
                            t_synth_end = time.time()
                            meta = getattr(self.tts_manager, "last_synthesis_meta", {}) or {}

                        duration = 1.8
                        if audio_path and os.path.exists(audio_path):
                            try:
                                with wave.open(str(audio_path), "rb") as wf:
                                    duration = wf.getnframes() / float(wf.getframerate())
                            except Exception:
                                duration = 1.8

                        t_play_start = time.time()
                        t_recv = getattr(item, "received_at", t_dequeued)
                        turn_ttfs = max(0.0, (t_play_start - t_dequeued) * 1000.0)
                        e2e_ttfs = max(0.0, (t_play_start - t_recv) * 1000.0)

                        rec = latency_tracker.record(
                            user_name=item.user_name,
                            text=item.text,
                            speaker_name=spk_name,
                            provider=meta.get("provider", "unknown"),
                            worker_label=meta.get("worker", "unknown"),
                            t_discord_created=getattr(item, "discord_created_at", 0.0),
                            t_received=t_recv,
                            t_queued=getattr(item, "queued_at", 0.0),
                            t_dequeued=t_dequeued,
                            t_synth_start=t_synth_start,
                            t_synth_end=t_synth_end,
                            t_play_start=t_play_start,
                            is_cached=meta.get("is_cached", False),
                            is_fallback=meta.get("is_fallback", False),
                            turn_ttfs_ms=round(turn_ttfs, 1),
                            e2e_ttfs_ms=round(e2e_ttfs, 1),
                            total_audio_duration_ms=round(duration * 1000.0, 1),
                        )
                        results.append(asdict(rec))
                        play_wait = min(duration, 2.0)
                        await asyncio.sleep(play_wait)

                except Exception as e:
                    logger.error(f"Error in test playback: {e}")
                finally:
                    test_queue.task_done()

        player_task = safe_create_task(_test_player_worker(), name="test_playback_worker")

        # メッセージ投入ループ (Discord検知を模倣)
        for msg in messages:
            t_now = time.time()
            t_created = t_now - 0.04  # 40ms前のDiscord Gateway配信を模倣
            t_received = t_now

            raw_text = msg.get("text", "")
            cleaned = vc_cog.text_cleaner.clean(
                raw_text=raw_text,
                guild=guild,
                dictionary_entries={},
                max_length=150
            )

            author_prefix = msg.get("author_prefix")

            item = QueueItem(
                text=cleaned,
                user_id=msg.get("user_id", 1000),
                user_name=msg.get("user_name", "User"),
                speaker_id=msg.get("speaker_id", "vv_2"),
                pitch=msg.get("pitch", "0%"),
                speed=msg.get("speed", 1.0),
                volume=0.8,
                author_prefix=author_prefix,
                discord_created_at=t_created,
                received_at=t_received,
                queued_at=time.time()
            )
            if mode != "streaming":
                item.synth_task = safe_create_task(
                    vc_cog._prefetch_audio(item),
                    name=f"test_prefetch_{item.user_id}_{int(time.time()*1000)}"
                )
            await test_queue.put(item)

            interval = msg.get("interval_seconds", 0.8)
            if interval > 0:
                await asyncio.sleep(interval)

        # 終了シグナル
        await test_queue.put(None)
        await player_task

        summary = latency_tracker.get_summary()
        return web.json_response({
            "success": True,
            "total_processed": len(results),
            "results": results,
            "latency_summary": summary
        })

    async def api_reset_latency(self, request: web.Request) -> web.Response:
        """レイテンシモニターの集計メトリクスを手動リセット"""
        guild, perms = await self._get_guild_and_perms(request, request.query.get("guild_id"))
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({"error": "Access denied."}, status=403)
        clear_hist = request.query.get("clear_history", "0") in ("1", "true")
        res = latency_tracker.reset(clear_history=clear_hist)
        return web.json_response(res)

    async def api_get_latency_incidents(self, request: web.Request) -> web.Response:
        """遅延・アンダーラン等の状態悪化インシデント履歴 (原因調査用)"""
        guild, perms = await self._get_guild_and_perms(request, request.query.get("guild_id"))
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({"error": "Access denied."}, status=403)
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError:
            limit = 50
        incidents = []
        if self.db and hasattr(self.db, "get_latency_incidents"):
            incidents = await self.db.get_latency_incidents(limit)
        if not incidents:
            incidents = latency_tracker.get_recent_incidents()[:limit]
        return web.json_response({"incidents": incidents, "count": len(incidents)})

    async def api_purge_cache(self, request: web.Request) -> web.Response:
        """システム全体のキャッシュ消去（管理者のみ）"""
        await self._require_admin(request)

        target = request.query.get("target", "all")
        count = await self.cache_manager.purge_all(target=target)
        return web.json_response({"success": True, "purged_files": count, "target": target})

    async def api_scan_cache(self, request: web.Request) -> web.Response:
        """キャッシュファイルの整合性スキャン（破損・無音ファイルの検出・自己修復）"""
        guild, perms = await self._get_guild_and_perms(request, request.query.get("guild_id"))
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({"error": "Access denied."}, status=403)

        result = await self.cache_manager.scan_and_cleanup_corrupted()
        return web.json_response({"success": True, "result": result})

    async def api_get_cache_stats_summary(self, request: web.Request) -> web.Response:
        """キャッシュ統計ダッシュボードサマリー集計 (D-2)"""
        user = self._require_auth(request)
        if not user:
            return web.json_response({"error": "Unauthorized"}, status=401)

        db_summary = {}
        if self.db:
            try:
                db_summary = await self.db.get_cache_stats_summary()
            except Exception as e:
                logger.warning(f"Error fetching cache stats summary from DB: {e}")

        # L1メモリキャッシュ情報
        l1_cache = getattr(self.cache_manager, "_l1_cache", None) or getattr(self.cache_manager, "l1_cache", {})
        l1_info = {
            "l1_items": len(l1_cache),
            "l1_max": getattr(self.cache_manager, "l1_max_items", 100),
            "l1_bytes": getattr(self.cache_manager, "_l1_current_bytes", getattr(self.cache_manager, "l1_current_bytes", 0)),
        }

        # クラウド・ローカルのディスク使用量
        if hasattr(self.cache_manager, "get_stats"):
            stats = self.cache_manager.get_stats()
            cloud_bytes = stats.get("cloud", {}).get("size_bytes", 0)
            local_bytes = stats.get("voicevox", {}).get("size_bytes", 0)
        else:
            cloud_dir = getattr(self.cache_manager, "cloud_dir", getattr(self.cache_manager, "cache_dir", None))
            local_dir = getattr(self.cache_manager, "local_dir", getattr(self.cache_manager, "voicevox_cache_dir", None))
            cloud_bytes = self.cache_manager.get_total_cache_size(cloud_dir) if hasattr(self.cache_manager, "get_total_cache_size") else 0
            local_bytes = self.cache_manager.get_total_cache_size(local_dir) if hasattr(self.cache_manager, "get_total_cache_size") else 0

        max_disk_bytes = getattr(self.cache_manager, "max_size_bytes", 0) + getattr(self.cache_manager, "voicevox_max_size_bytes", 0)

        return web.json_response({
            "success": True,
            "db_summary": db_summary,
            "l1_info": l1_info,
            "disk_info": {
                "cloud_bytes": cloud_bytes,
                "local_bytes": local_bytes,
                "total_disk_bytes": cloud_bytes + local_bytes,
                "max_disk_bytes": max_disk_bytes
            }
        })

    async def api_list_cache_files(self, request: web.Request) -> web.Response:
        """キャッシュファイル一覧取得（検索・ソート・ページネーション対応）"""
        guild, perms = await self._get_guild_and_perms(request, request.query.get("guild_id"))
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({"error": "Access denied."}, status=403)

        target = request.query.get("target", "cloud")
        search = request.query.get("search", "").strip()
        sort_by = request.query.get("sort_by", "mtime")
        sort_order = request.query.get("sort_order", "desc")
        try:
            page = int(request.query.get("page", 1))
        except ValueError:
            page = 1
        try:
            per_page = int(request.query.get("per_page", 30))
        except ValueError:
            per_page = 30

        result = await self.cache_manager.list_files(
            target=target,
            page=page,
            per_page=per_page,
            search=search,
            sort_by=sort_by,
            sort_order=sort_order
        )
        return web.json_response(result)

    async def api_get_cache_audio(self, request: web.Request) -> web.Response:
        """キャッシュされたWAV音声をインライン再生またはダウンロード用に配信"""
        user = self._require_auth(request)
        target = request.match_info.get("target", "cloud")
        filename = request.match_info.get("filename", "")
        
        clean_name = sanitize_filename(filename)
        if not clean_name or not clean_name.endswith(".wav"):
            return web.Response(text="Invalid filename", status=400)

        base_cache_dir = self.cache_manager.voicevox_cache_dir if target in ("voicevox", "local") else self.cache_manager.cache_dir
        file_path = validate_safe_path(base_cache_dir, clean_name)
        if not file_path or not file_path.exists() or not file_path.is_file():
            return web.Response(text="Cache file not found", status=404)

        download = request.query.get("download", "0") == "1"
        disposition = f'attachment; filename="{clean_name}"' if download else f'inline; filename="{clean_name}"'

        headers = {
            "Content-Type": "audio/wav",
            "Content-Disposition": disposition,
            "Cache-Control": "public, max-age=3600"
        }
        return web.FileResponse(file_path, headers=headers)

    async def api_delete_cache_file(self, request: web.Request) -> web.Response:
        """キャッシュファイルの個別削除"""
        guild, perms = await self._get_guild_and_perms(request, request.query.get("guild_id"))
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({"error": "Access denied."}, status=403)

        target = request.match_info.get("target", "cloud")
        filename = request.match_info.get("filename", "")
        clean_name = sanitize_filename(filename)
        if not clean_name or not clean_name.endswith(".wav"):
            return web.json_response({"error": "Invalid filename"}, status=400)

        success = self.cache_manager.delete_file(target, clean_name)
        if not success:
            return web.json_response({"error": "File not found or failed to delete"}, status=400)
        return web.json_response({"success": True, "filename": clean_name, "target": target})

    async def api_delete_cache_files_batch(self, request: web.Request) -> web.Response:
        """キャッシュファイルの一括削除"""
        data = await request.json()
        guild, perms = await self._get_guild_and_perms(request, data.get("guild_id") or request.query.get("guild_id"))
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({"error": "Access denied."}, status=403)

        target = data.get("target", "cloud")
        filenames = data.get("filenames", [])
        if not isinstance(filenames, list) or not filenames:
            return web.json_response({"error": "No filenames specified"}, status=400)

        deleted_count = await self.cache_manager.delete_files_batch(target, filenames)
        return web.json_response({"success": True, "deleted_count": deleted_count, "target": target})

    async def api_get_models_status(self, request: web.Request) -> web.Response:
        """各種エンジン状態とCOEIROINK公認モデルの導入状況"""
        guild, perms = await self._get_guild_and_perms(request, request.query.get("guild_id"))
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({"error": "Access denied."}, status=403)

        engines = {
            "voicevox": {"available": self.tts_manager.voicevox_client is not None, "url": self.config.voicevox_base_url},
            "sharevox": {"available": self.tts_manager.sharevox_client is not None, "url": self.config.sharevox_base_url},
            "coeiroink": {"available": self.tts_manager.coeiroink_client is not None, "url": self.config.coeiroink_base_url},
            "openjtalk": {"available": getattr(self.tts_manager, "openjtalk_client", None) is not None and self.tts_manager.openjtalk_client.is_available()},
            "edge": {"available": self.tts_manager.edge_client is not None},
            "azure": {"available": bool(self.config.azure_speech_key)},
            "google": {"available": bool(self.config.google_credentials_json or os.path.exists("gcp-key.json"))}
        }

        coeiroink_status = await asyncio.to_thread(self.coeiroink_model_manager.get_status)

        voice_counts = {
            "total": len(self.config.voices),
            "azure": sum(1 for v in self.config.voices.values() if v.provider == "azure"),
            "google": sum(1 for v in self.config.voices.values() if v.provider == "google"),
            "edge": sum(1 for v in self.config.voices.values() if v.provider == "edge"),
            "voicevox": sum(1 for v in self.config.voices.values() if v.provider == "voicevox"),
            "sharevox": sum(1 for v in self.config.voices.values() if v.provider == "sharevox"),
            "coeiroink": sum(1 for v in self.config.voices.values() if v.provider == "coeiroink"),
            "openjtalk": sum(1 for v in self.config.voices.values() if v.provider == "openjtalk"),
        }

        return web.json_response({
            "engines": engines,
            "coeiroink": coeiroink_status,
            "voice_counts": voice_counts
        })

    async def api_trigger_model_update(self, request: web.Request) -> web.Response:
        """モデルのダウンロード・更新を開始"""
        guild, perms = await self._get_guild_and_perms(request, request.query.get("guild_id"))
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({"error": "Access denied."}, status=403)

        if self.coeiroink_model_manager.is_downloading:
            return web.json_response({"error": "Download is already in progress."}, status=400)

        data = await request.json() if request.can_read_body else {}
        action = data.get("action", "all")
        prefix = data.get("prefix")

        async def _run_download():
            if action == "speaker" and prefix:
                await self.coeiroink_model_manager.download_speaker(prefix)
            else:
                await self.coeiroink_model_manager.download_all()

            try:
                await self.tts_manager.initialize_coeiroink()
            except Exception as e:
                logger.error(f"Failed to re-initialize COEIROINK voices: {e}")

        safe_create_task(_run_download(), name="download_model")
        return web.json_response({"success": True, "message": "Download task started."})

    async def api_get_model_progress(self, request: web.Request) -> web.Response:
        """モデルダウンロードの進捗状況を取得"""
        guild, perms = await self._get_guild_and_perms(request, request.query.get("guild_id"))
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({"error": "Access denied."}, status=403)

        return web.json_response(self.coeiroink_model_manager.progress)

    async def api_get_mycoeiroink_list(self, request: web.Request) -> web.Response:
        """MYCOEIROINK配布者リストの取得 (検索・ページネーション・導入状況判定)"""
        user = self._require_auth(request)
        q = request.query.get("q", "")
        try:
            page = int(request.query.get("page", 1))
        except (ValueError, TypeError):
            page = 1
        try:
            limit = int(request.query.get("limit", 30))
        except (ValueError, TypeError):
            limit = 30

        data = await self.mycoeiroink.get_list(query=q, page=page, limit=limit)

        # 現在ローカルCOEIROINKに導入済みのUUIDを抽出 (ディスク上の全 metas.json を直接走査 + config.voices)
        installed_uuids = set(self.mycoeiroink.get_installed_speaker_uuids())
        for v in self.config.voices.values():
            if getattr(v, "provider", "") == "coeiroink" and getattr(v, "voice_name", ""):
                parts = v.voice_name.split("_")
                if len(parts) >= 2:
                    u_norm = self.mycoeiroink.normalize_uuid(parts[0])
                    if u_norm:
                        installed_uuids.add(u_norm)
        data["installed_uuids"] = sorted(list(installed_uuids))

        return web.json_response(data)

    async def api_sync_mycoeiroink(self, request: web.Request) -> web.Response:
        """COEIROINK公式サイトから最新のMYCOEIROINK配布情報を一括同期"""
        user = self._require_auth(request)
        guild, perms = await self._get_guild_and_perms(request, request.query.get("guild_id"))
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({"error": "Access denied. 管理者または指定ロールが必要です。"}, status=403)

        try:
            res = await self.mycoeiroink.sync_all_from_official()
            return web.json_response(res)
        except Exception as e:
            logger.error(f"Error during MYCOEIROINK sync: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def api_get_mycoeiroink_character(self, request: web.Request) -> web.Response:
        """指定UUIDのMYCOEIROINK配布キャラクター詳細（任意取得）"""
        user = self._require_auth(request)
        uuid = request.match_info.get("uuid", "")
        char = await self.mycoeiroink.get_speaker_by_uuid(uuid)
        if not char:
            return web.json_response({"error": "Character not found"}, status=404)

        # 導入済みかどうかも判定 (ディスク走査 + config.voices)
        installed_uuids = set(self.mycoeiroink.get_installed_speaker_uuids())
        for v in self.config.voices.values():
            if getattr(v, "provider", "") == "coeiroink" and getattr(v, "voice_name", ""):
                parts = v.voice_name.split("_")
                if len(parts) >= 2:
                    u_norm = self.mycoeiroink.normalize_uuid(parts[0])
                    if u_norm:
                        installed_uuids.add(u_norm)

        target_u = self.mycoeiroink.normalize_uuid(char.get("speaker_uuid", ""))
        char["is_installed"] = target_u in installed_uuids

        return web.json_response({"character": char})

    async def api_install_auto_mycoeiroink(self, request: web.Request) -> web.Response:
        """MYCOEIROINK 配布音声の自動導入を試行（管理者のみ）"""
        await self._require_admin(request)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        speaker_uuid = data.get("speaker_uuid", "").strip()
        if not speaker_uuid:
            return web.json_response({"error": "speaker_uuid is required"}, status=400)

        res = await self.mycoeiroink.try_auto_install(speaker_uuid)
        if res.get("success"):
            # COEIROINK の初期化を再実行して config.voices に最新モデルを登録
            if hasattr(self, "tts_manager") and self.tts_manager:
                safe_create_task(self.tts_manager.initialize_coeiroink(), name="init_coeiroink_auto")
        return web.json_response(res)

    async def api_upload_mycoeiroink_zip(self, request: web.Request) -> web.Response:
        """ユーザーが手動ダウンロードしたZIPファイルをWebUI経由で安全にアップロード・配置（管理者のみ）"""
        await self._require_admin(request)
        try:
            reader = await request.multipart()
            field = await reader.next()
            if not field or field.name != "file":
                logger.warning("[MYCOEIROINK Upload Rejected] アップロードするZIPファイルが指定されていません。")
                return web.json_response({"error": "アップロードするZIPファイルが指定されていません。"}, status=400)

            filename = getattr(field, "filename", "unknown.zip")
            speaker_uuid_hint = request.query.get("speaker_uuid")
            logger.info(f"[MYCOEIROINK Upload] アップロード受信開始: {filename} (target_uuid: {speaker_uuid_hint or '自動判定'})")

            chunks = []
            size = 0
            max_size = 500 * 1024 * 1024  # 500MB
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                if size > max_size:
                    logger.warning(f"[MYCOEIROINK Upload Rejected] ファイルサイズ上限超過 ({filename}): {size / 1024 / 1024:.1f}MB > 500MB")
                    return web.json_response({"error": "ファイルサイズが上限 (500MB) を超過しています。"}, status=400)
                chunks.append(chunk)

            zip_bytes = b"".join(chunks)
            logger.info(f"[MYCOEIROINK Upload] 受信完了 ({len(zip_bytes) / 1024 / 1024:.2f} MB)。セキュリティ検証およびZIP展開を開始します...")

            res = self.mycoeiroink.safe_extract_zip(zip_bytes, speaker_uuid_hint=speaker_uuid_hint)
            logger.info(f"[MYCOEIROINK Upload Success] モデル展開完了: {res.get('speaker_name', '不明')} (UUID: {res.get('speaker_uuid')}, スタイル数: {len(res.get('styles', []))})")

            # COEIROINK の初期化を再実行して config.voices に最新モデルを登録
            if hasattr(self, "tts_manager") and self.tts_manager:
                safe_create_task(self.tts_manager.initialize_coeiroink(), name="init_coeiroink_zip")

            return web.json_response(res)
        except ValueError as ve:
            logger.warning(f"[MYCOEIROINK Upload Rejected] バリデーション失敗: {ve}")
            return web.json_response({"error": str(ve)}, status=400)
        except Exception as e:
            logger.error(f"[MYCOEIROINK Upload Error] 展開処理中の予期せぬ例外: {e}", exc_info=True)
            return web.json_response({"error": f"展開処理中にエラーが発生しました: {e}"}, status=500)

    async def api_upload_mycoeiroink_chunk(self, request: web.Request) -> web.Response:
        """
        Cloudflare の 100MB 制限を突破するためのチャンク分割アップロードAPI（管理者のみ）。
        ブラウザから 20MB 単位で分割送信されたバイナリを一時保存し、最終チャンクで結合・安全展開を実行。
        """
        await self._require_admin(request)
        chunk_dir = None
        try:
            reader = await request.multipart()
            upload_id = None
            chunk_index = -1
            total_chunks = -1
            filename = "unknown.zip"
            speaker_uuid = None
            chunk_data = None

            while True:
                part = await reader.next()
                if part is None:
                    break
                name = part.name
                if name == "upload_id":
                    upload_id = (await part.text()).strip()
                elif name == "chunk_index":
                    chunk_index = int((await part.text()).strip())
                elif name == "total_chunks":
                    total_chunks = int((await part.text()).strip())
                elif name == "filename":
                    filename = (await part.text()).strip()
                elif name == "speaker_uuid":
                    speaker_uuid = (await part.text()).strip() or None
                elif name == "chunk":
                    chunk_data = await part.read()

            if not upload_id or chunk_index < 0 or total_chunks <= 0 or chunk_data is None:
                logger.warning(f"[MYCOEIROINK Chunk Upload Rejected] パラメータ不足 (id={upload_id}, index={chunk_index}, total={total_chunks})")
                return web.json_response({"error": "チャンクアップロードのパラメータが不足しています。"}, status=400)

            # パストラバーサル防止のため upload_id を検証
            import re
            if not re.match(r"^[a-zA-Z0-9_\-]+$", upload_id):
                return web.json_response({"error": "無効な upload_id です。"}, status=400)

            chunk_dir = Path("/app/cache/upload_chunks") / upload_id
            chunk_dir.mkdir(parents=True, exist_ok=True)
            chunk_file = chunk_dir / f"chunk_{chunk_index:05d}"
            chunk_file.write_bytes(chunk_data)

            logger.info(
                f"[MYCOEIROINK Chunk Upload] 受信完了: パート {chunk_index + 1}/{total_chunks} "
                f"({len(chunk_data) / 1024 / 1024:.1f} MB, file: {filename})"
            )

            # 最終チャンクか判定
            if chunk_index + 1 == total_chunks:
                logger.info(f"[MYCOEIROINK Chunk Upload] 全 {total_chunks} パート受信完了。結合を開始します ({filename})...")
                # 全チャンクの存在確認と結合
                combined_chunks = []
                total_size = 0
                for i in range(total_chunks):
                    cf = chunk_dir / f"chunk_{i:05d}"
                    if not cf.exists():
                        raise ValueError(f"チャンク欠落を検知しました (chunk_{i:05d} が見つかりません)")
                    c_bytes = cf.read_bytes()
                    total_size += len(c_bytes)
                    if total_size > 500 * 1024 * 1024:
                        raise ValueError(f"結合後のファイルサイズが上限 (500MB) を超過しています: {total_size / 1024 / 1024:.1f}MB")
                    combined_chunks.append(c_bytes)

                combined_zip = b"".join(combined_chunks)
                logger.info(f"[MYCOEIROINK Chunk Upload] 結合完了 ({len(combined_zip) / 1024 / 1024:.2f} MB)。安全展開を実施します...")

                # 一時ディレクトリのクリーンアップ
                import shutil
                shutil.rmtree(chunk_dir, ignore_errors=True)
                chunk_dir = None

                # 安全展開とモデル配置
                res = self.mycoeiroink.safe_extract_zip(combined_zip, speaker_uuid_hint=speaker_uuid)
                logger.info(
                    f"[MYCOEIROINK Upload Success] モデル展開完了: {res.get('speaker_name', '不明')} "
                    f"(UUID: {res.get('speaker_uuid')}, スタイル数: {len(res.get('styles', []))})"
                )

                # COEIROINK 再初期化タスク
                if hasattr(self, "tts_manager") and self.tts_manager:
                    safe_create_task(self.tts_manager.initialize_coeiroink(), name="init_coeiroink_chunk")

                return web.json_response(res)

            return web.json_response({
                "success": True,
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
                "status": "chunk_received"
            })
        except ValueError as ve:
            logger.warning(f"[MYCOEIROINK Chunk Upload Rejected] バリデーション失敗: {ve}")
            if chunk_dir and chunk_dir.exists():
                import shutil
                shutil.rmtree(chunk_dir, ignore_errors=True)
            return web.json_response({"error": str(ve)}, status=400)
        except Exception as e:
            logger.error(f"[MYCOEIROINK Chunk Upload Error] チャンク処理例外: {e}", exc_info=True)
            if chunk_dir and chunk_dir.exists():
                import shutil
                shutil.rmtree(chunk_dir, ignore_errors=True)
            return web.json_response({"error": f"アップロード処理中にエラーが発生しました: {e}"}, status=500)

    async def api_trigger_disaster_test(self, request: web.Request) -> web.Response:
        """防災速報のテスト発信"""
        user = self._require_auth(request)
        try:
            data = await request.json()
        except Exception:
            data = {}

        guild_id_str = data.get("guild_id") or request.query.get("guild_id")
        guild, perms = await self._get_guild_and_perms(request, guild_id_str)
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({"error": "Access denied."}, status=403)

        alert_type = data.get("type", "earthquake")
        if not self.disaster_service:
            return web.json_response({"error": "防災サービスが初期化されていません。"}, status=503)

        sent_count = await self.disaster_service.trigger_test_alert(guild.id, alert_type=alert_type)
        return web.json_response({
            "status": "ok",
            "message": f"防災速報テスト（{alert_type}）を発信しました。送信先VC数: {sent_count}",
            "sent_count": sent_count
        })

    async def api_update_disaster_config(self, request: web.Request) -> web.Response:
        """ギルドの防災速報設定の更新"""
        user = self._require_auth(request)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        guild_id = data.get("guild_id")
        if not guild_id:
            return web.json_response({"error": "guild_id is required"}, status=400)

        guild, perms = await self._get_guild_and_perms(request, str(guild_id))
        if not (perms["is_admin"] or perms["has_role"]):
            return web.json_response({"error": "Access denied."}, status=403)

        updates = {}
        if "disaster_alert_enabled" in data:
            updates["disaster_alert_enabled"] = bool(data["disaster_alert_enabled"])
        if "disaster_min_scale" in data:
            try:
                updates["disaster_min_scale"] = int(data["disaster_min_scale"])
            except ValueError:
                pass
        if "disaster_pref_filter" in data:
            updates["disaster_pref_filter"] = str(data["disaster_pref_filter"] or "")
        if "disaster_alert_weather" in data:
            updates["disaster_alert_weather"] = bool(data["disaster_alert_weather"])
        if "disaster_volume" in data:
            try:
                updates["disaster_volume"] = max(0.0, min(float(data["disaster_volume"]), 2.0))
            except (ValueError, TypeError):
                pass

        if updates:
            await self.db.update_guild_settings(int(guild_id), **updates)

        updated_settings = await self.db.get_guild_settings(int(guild_id))
        return web.json_response({
            "status": "ok",
            "settings": {
                "disaster_alert_enabled": bool(updated_settings.get("disaster_alert_enabled", 1)),
                "disaster_min_scale": updated_settings.get("disaster_min_scale", 30),
                "disaster_pref_filter": updated_settings.get("disaster_pref_filter", ""),
                "disaster_alert_weather": bool(updated_settings.get("disaster_alert_weather", 1)),
                "disaster_volume": float(updated_settings.get("disaster_volume", 1.0) if updated_settings.get("disaster_volume") is not None else 1.0),
            }
        })


    async def api_get_logs(self, request: web.Request) -> web.Response:
        """システム全体のログ取得（管理者のみ）"""
        await self._require_admin(request)

        log_type = request.query.get("type", "bot")
        if log_type == "web":
            log_path = Path(self.config.web_log_file)
        elif log_type == "engine":
            log_path = Path(getattr(self.config, "engine_log_file", "/app/logs/engine.log"))
        else:
            log_path = Path(self.config.log_file)
        lines = []
        if log_path.exists():
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
                    lines = all_lines[-50:]  # 最新50行
            except Exception as e:
                lines = [f"Error reading logs: {e}"]
        else:
            lines = [f"ログファイル ({log_path.name}) はまだ作成されていません。"]
        return web.json_response({"logs": lines, "type": log_type})

    async def api_get_character_terms(self, request: web.Request) -> web.Response:
        """
        全音声キャラクターの推奨クレジット表記および個別利用規約リンク一覧を取得。
        静的ベースリストに加え、ローカルに導入されたMYCOEIROINKモデルを自動走査・統合して返却。
        """
        # 利用規約情報は公開情報のため未認証でもアクセス可能

        base_terms: List[Dict[str, str]] = [
            # --- VOICEVOX ---
            {"name": "四国めたん (VOICEVOX)", "credit": "VOICEVOX:四国めたん", "creator": "SSS合同会社", "url": "https://zunko.jp/guideline.html"},
            {"name": "ずんだもん (VOICEVOX)", "credit": "VOICEVOX:ずんだもん", "creator": "SSS合同会社", "url": "https://zunko.jp/guideline.html"},
            {"name": "春日部つむぎ (VOICEVOX)", "credit": "VOICEVOX:春日部つむぎ", "creator": "春日部つむぎ公式", "url": "https://tsumugi-official.studio.site/rule"},
            {"name": "雨晴はう (VOICEVOX)", "credit": "VOICEVOX:雨晴はう", "creator": "雨晴はう公式", "url": "https://ameharehau.wixsite.com/website/guideline"},
            {"name": "波音リツ (VOICEVOX)", "credit": "VOICEVOX:波音リツ", "creator": "Canon", "url": "http://canon-voice.sakura.ne.jp/kiyaku.html"},
            {"name": "玄野武宏 (VOICEVOX)", "credit": "VOICEVOX:玄野武宏", "creator": "VIRVOX PROJECT", "url": "https://virvoxproject.wixsite.com/official/terms"},
            {"name": "白上虎太郎 (VOICEVOX)", "credit": "VOICEVOX:白上虎太郎", "creator": "VIRVOX PROJECT", "url": "https://virvoxproject.wixsite.com/official/terms"},
            {"name": "青山龍星 (VOICEVOX)", "credit": "VOICEVOX:青山龍星", "creator": "VIRVOX PROJECT", "url": "https://virvoxproject.wixsite.com/official/terms"},
            {"name": "冥鳴ひまり (VOICEVOX)", "credit": "VOICEVOX:冥鳴ひまり", "creator": "冥鳴ひまり公式", "url": "https://meimeihimari.wixsite.com/himari"},
            {"name": "九州そら (VOICEVOX)", "credit": "VOICEVOX:九州そら", "creator": "SSS合同会社", "url": "https://zunko.jp/guideline.html"},
            {"name": "もち子さん (VOICEVOX)", "credit": "VOICEVOX:もち子さん", "creator": "明日葉友恵", "url": "https://vturing.fanbox.cc/posts/2622748"},
            {"name": "剣崎雌雄 (VOICEVOX)", "credit": "VOICEVOX:剣崎雌雄", "creator": "VIRVOX PROJECT", "url": "https://virvoxproject.wixsite.com/official/terms"},
            {"name": "WhiteCUL (VOICEVOX)", "credit": "VOICEVOX:WhiteCUL", "creator": "CUL PROJECT", "url": "https://www.jcm-cul.jp/whitecul/"},
            {"name": "後鬼 (VOICEVOX)", "credit": "VOICEVOX:後鬼", "creator": "VIRVOX PROJECT", "url": "https://virvoxproject.wixsite.com/official/terms"},
            {"name": "No.7 (VOICEVOX)", "credit": "VOICEVOX:No.7", "creator": "SEVEN", "url": "https://suzushiro-no7.tumblr.com/terms"},
            {"name": "ちび式じい (VOICEVOX)", "credit": "VOICEVOX:ちび式じい", "creator": "VIRVOX PROJECT", "url": "https://virvoxproject.wixsite.com/official/terms"},
            {"name": "櫻歌ミコ (VOICEVOX)", "credit": "VOICEVOX:櫻歌ミコ", "creator": "櫻歌ミコ公式", "url": "https://miko35.info/terms/"},
            {"name": "小夜/SAYO (VOICEVOX)", "credit": "VOICEVOX:小夜/SAYO", "creator": "小夜公式", "url": "https://315sayo.fanbox.cc/posts/3424103"},
            {"name": "ナースロボ＿タイプＴ (VOICEVOX)", "credit": "VOICEVOX:ナースロボ＿タイプＴ", "creator": "VIRVOX PROJECT", "url": "https://virvoxproject.wixsite.com/official/terms"},
            {"name": "†聖騎士 紅桜† (VOICEVOX)", "credit": "VOICEVOX:†聖騎士 紅桜†", "creator": "VIRVOX PROJECT", "url": "https://virvoxproject.wixsite.com/official/terms"},
            {"name": "雀松朱司 (VOICEVOX)", "credit": "VOICEVOX:雀松朱司", "creator": "VIRVOX PROJECT", "url": "https://virvoxproject.wixsite.com/official/terms"},
            {"name": "麒ヶ島宗麟 (VOICEVOX)", "credit": "VOICEVOX:麒ヶ島宗麟", "creator": "VIRVOX PROJECT", "url": "https://virvoxproject.wixsite.com/official/terms"},
            {"name": "春歌ナナ (VOICEVOX)", "credit": "VOICEVOX:春歌ナナ", "creator": "ななひら", "url": "http://haruka-nana.sakura.ne.jp/"},
            {"name": "猫使アル (VOICEVOX)", "credit": "VOICEVOX:猫使アル", "creator": "猫使公式", "url": "https://necousa-official.jimdofree.com/"},
            {"name": "猫使ビィ (VOICEVOX)", "credit": "VOICEVOX:猫使ビィ", "creator": "猫使公式", "url": "https://necousa-official.jimdofree.com/"},
            {"name": "中国うさぎ (VOICEVOX)", "credit": "VOICEVOX:中国うさぎ", "creator": "SSS合同会社", "url": "https://zunko.jp/guideline.html"},
            {"name": "栗田まろん (VOICEVOX)", "credit": "VOICEVOX:栗田まろん", "creator": "VIRVOX PROJECT", "url": "https://virvoxproject.wixsite.com/official/terms"},
            {"name": "あいえるたん (VOICEVOX)", "credit": "VOICEVOX:あいえるたん", "creator": "株式会社インフィニットループ", "url": "https://www.infiniteloop.co.jp/special/iltan/"},
            {"name": "満別花丸 (VOICEVOX)", "credit": "VOICEVOX:満別花丸", "creator": "VIRVOX PROJECT", "url": "https://virvoxproject.wixsite.com/official/terms"},
            {"name": "琴詠ニア (VOICEVOX)", "credit": "VOICEVOX:琴詠ニア", "creator": "VIRVOX PROJECT", "url": "https://virvoxproject.wixsite.com/official/terms"},

            # --- SHAREVOX ---
            {"name": "小春音アミ (SHAREVOX)", "credit": "SHAREVOX:小春音アミ", "creator": "小春音アミ公式", "url": "https://sharevox.app/characters/ami"},
            {"name": "つくよみちゃん (SHAREVOX)", "credit": "SHAREVOX:つくよみちゃん", "creator": "夢前黎", "url": "https://tyc.rei-yumesaki.net/material/rules/"},
            {"name": "らごぱすブラック (SHAREVOX)", "credit": "SHAREVOX:らごぱすブラック", "creator": "SHAREVOX Project", "url": "https://sharevox.app/terms"},
            {"name": "らごぱすホワイト (SHAREVOX)", "credit": "SHAREVOX:らごぱすホワイト", "creator": "SHAREVOX Project", "url": "https://sharevox.app/terms"},
            {"name": "白痴ー/黒聡鵜月 (SHAREVOX)", "credit": "SHAREVOX:白痴ー/黒聡鵜月", "creator": "SHAREVOX Project", "url": "https://sharevox.app/terms"},
            {"name": "Yくん/開発者 (SHAREVOX)", "credit": "SHAREVOX:Yくん/開発者", "creator": "SHAREVOX Project", "url": "https://sharevox.app/terms"},
            {"name": "風花ゆき (SHAREVOX)", "credit": "SHAREVOX:風花ゆき", "creator": "SHAREVOX Project", "url": "https://sharevox.app/terms"},
            {"name": "安倍広葉 (SHAREVOX)", "credit": "SHAREVOX:安倍広葉", "creator": "SHAREVOX Project", "url": "https://sharevox.app/terms"},
            {"name": "鈴乃 (SHAREVOX)", "credit": "SHAREVOX:鈴乃", "creator": "SHAREVOX Project", "url": "https://sharevox.app/terms"},

            # --- COEIROINK 公式 ---
            {"name": "つくよみちゃん (COEIROINK)", "credit": "COEIROINK:つくよみちゃん", "creator": "夢前黎", "url": "https://tyc.rei-yumesaki.net/material/rules/"},
            {"name": "MANA (COEIROINK)", "credit": "COEIROINK:MANA", "creator": "鹿仲茉菜", "url": "https://coeiroink.com/character/official/mana"},
            {"name": "KANA (COEIROINK)", "credit": "COEIROINK:KANA", "creator": "眞中佳奈", "url": "https://coeiroink.com/character/official/kana"},
            {"name": "おふとんP (COEIROINK)", "credit": "COEIROINK:おふとんP", "creator": "おふとんP", "url": "https://coeiroink.com/character/official/ofuton-p"},
            {"name": "アメノちゃん (COEIROINK)", "credit": "COEIROINK:アメノちゃん", "creator": "アメノちゃん公式", "url": "https://coeiroink.com/character/official/ameno"},
            {"name": "ディアちゃん (COEIROINK)", "credit": "COEIROINK:ディアちゃん", "creator": "ディアちゃん公式", "url": "https://coeiroink.com/character/official/dia"},
            {"name": "アルマちゃん (COEIROINK)", "credit": "COEIROINK:アルマちゃん", "creator": "アルマちゃん公式", "url": "https://coeiroink.com/character/official/arma"},
            {"name": "リリンちゃん (COEIROINK)", "credit": "COEIROINK:リリンちゃん", "creator": "リリンちゃん公式", "url": "https://coeiroink.com/character/official/lilin"},
            {"name": "クロワちゃん (COEIROINK)", "credit": "COEIROINK:クロワちゃん", "creator": "クロワちゃん公式", "url": "https://coeiroink.com/character/official/croix"},
            {"name": "アンジーさん (COEIROINK)", "credit": "COEIROINK:アンジーさん", "creator": "アンジーさん公式", "url": "https://coeiroink.com/character/official/andzie"},
            {"name": "AI声優-朱花 (COEIROINK)", "credit": "COEIROINK:AI声優-朱花", "creator": "AI声優公式", "url": "https://coeiroink.com/character/official/ayaka"},
            {"name": "AI声優-青葉 (COEIROINK)", "credit": "COEIROINK:AI声優-青葉", "creator": "AI声優公式", "url": "https://coeiroink.com/character/official/aoba"},
            {"name": "AI声優-銀芽 (COEIROINK)", "credit": "COEIROINK:AI声優-銀芽", "creator": "AI声優公式", "url": "https://coeiroink.com/character/official/ginga"},
            {"name": "AI声優-金苗 (COEIROINK)", "credit": "COEIROINK:AI声優-金苗", "creator": "AI声優公式", "url": "https://coeiroink.com/character/official/kanae"},

            # --- OpenJTalk ---
            {"name": "メイ (OpenJTalk)", "credit": "Open JTalk:メイ", "creator": "名古屋工業大学 MMDAgent", "url": "https://www.mmdagent.jp/"},
            {"name": "たくみ (OpenJTalk)", "credit": "Open JTalk:たくみ", "creator": "名古屋工業大学 MMDAgent", "url": "https://www.mmdagent.jp/"},
            {"name": "名工大 男声 (OpenJTalk)", "credit": "Open JTalk:nitech_jp_atr503_m001", "creator": "名古屋工業大学 / ATR", "url": "http://open-jtalk.sourceforge.net/"},
            {"name": "ゆっくり風 (OpenJTalk)", "credit": "Open JTalk (機械調)", "creator": "名古屋工業大学 MMDAgent", "url": "http://open-jtalk.sourceforge.net/"}
        ]

        existing_names = {t["name"].split(" (")[0] for t in base_terms}

        # MYCOEIROINK キャッシュデータ（公式配布リスト）のインデックス
        items_by_uuid: Dict[str, Any] = {}
        if hasattr(self.mycoeiroink, "_cache_data") and self.mycoeiroink._cache_data:
            for it in self.mycoeiroink._cache_data.get("items", []):
                u = self.mycoeiroink.normalize_uuid(it.get("speaker_uuid", ""))
                if u:
                    items_by_uuid[u] = it

        # ローカル導入済みモデルフォルダの走査
        installed_mycoe_terms: List[Dict[str, str]] = []
        if self.mycoeiroink.model_dir.exists():
            for entry in self.mycoeiroink.model_dir.iterdir():
                if entry.is_dir():
                    mf = entry / "metas.json"
                    if mf.exists():
                        try:
                            with open(mf, "r", encoding="utf-8") as f:
                                meta = json.load(f)
                                name = meta.get("speakerName", "").strip()
                                uuid = self.mycoeiroink.normalize_uuid(str(meta.get("speakerUuid", "")).strip())
                                if not name or name in existing_names:
                                    continue

                                matched = items_by_uuid.get(uuid)
                                creator = "MYCOEIROINK配布ライブラリ"
                                url = "https://coeiroink.com/my-coeiroink"
                                if matched:
                                    if matched.get("url"):
                                        url = matched["url"]
                                        try:
                                            parsed = urllib.parse.urlparse(url)
                                            if "booth.pm" in parsed.netloc:
                                                creator = f"BOOTH ({parsed.netloc.split('.')[0]})"
                                        except Exception:
                                            pass

                                existing_names.add(name)
                                installed_mycoe_terms.append({
                                    "name": f"{name} (MYCOEIROINK)",
                                    "credit": f"COEIROINK:{name}",
                                    "creator": creator,
                                    "url": url
                                })
                        except Exception as e:
                            logger.debug(f"Failed to parse metas.json in {entry}: {e}")

        # 名前順でソートして結合
        all_terms = base_terms + sorted(installed_mycoe_terms, key=lambda x: x["name"])
        return web.json_response({"terms": all_terms})

    # --- 管理者向け メンバー個人音声設定 API ---

    async def api_get_avatar(self, request: web.Request) -> web.Response:
        """
        ユーザーのAVIFアバター画像をローカルキャッシュから配信。
        未キャッシュの場合は 404 を返し、フロントエンド側でCSSフォールバックする。
        """
        uid_str = request.match_info.get("user_id", "")
        try:
            uid = int(uid_str)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid user ID")

        if not has_avatar(uid):
            raise web.HTTPNotFound(text="Avatar not cached")

        path = get_avatar_path(uid)
        try:
            with open(path, "rb") as f:
                data = f.read()
            return web.Response(
                body=data,
                content_type="image/avif",
                headers={
                    "Cache-Control": "public, max-age=86400",
                }
            )
        except Exception as e:
            logger.error(f"Failed to read avatar file {path}: {e}")
            raise web.HTTPInternalServerError()

    async def api_admin_get_guild_users(self, request: web.Request) -> web.Response:
        """
        管理者専用: 該当サーバーで個人音声設定が保存されているユーザー一覧を取得。
        """
        guild_id_str = request.query.get("guild_id")
        guild, perms = await self._get_guild_and_perms(request, guild_id_str)
        if not (perms["is_admin"] or perms["has_role"]):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Access denied. Only administrators and assigned roles can access this page."}),
                content_type="application/json"
            )

        settings_list = await self.db.get_all_guild_user_settings_for_guild(guild.id)
        cached_members = await self.db.get_all_guild_member_caches_for_guild(guild.id)

        # 外部API (Discord REST/Gateway) 通信を一切行わず、ローカルメモリキャッシュおよびDBキャッシュから即座に解決
        # これによりGatewayチャンクリクエスト・429レートリミットを100%根絶しつつ、最新情報とAVIFアバターを提供
        def resolve_user_info(st: Dict[str, Any]) -> Dict[str, Any]:
            uid = st["user_id"]
            member = guild.get_member(uid)
            cached = cached_members.get(uid)

            username = f"ユーザー ({uid})"
            display_name = f"ユーザー ({uid})"
            is_in_guild = False

            if member:
                # 直近アクティブ（Botメモリ上に実在）
                display_name = member.display_name
                username = member.name
                is_in_guild = True
            elif cached:
                # 当該サーバーのDBキャッシュから復元（在室フラグ、サーバーニックネーム）
                display_name = cached.get("display_name") or cached.get("username") or f"ユーザー ({uid})"
                username = cached.get("username") or f"ユーザー ({uid})"
                is_in_guild = cached.get("is_in_guild", False)
            else:
                user = self.bot.get_user(uid)
                if user:
                    display_name = getattr(user, "global_name", None) or user.name
                    username = user.name

            speaker_id = st.get("speaker_id", "")
            voice_info = self.config.voices.get(speaker_id)
            speaker_name = getattr(voice_info, "name", speaker_id) if voice_info else speaker_id
            provider = getattr(voice_info, "provider", "UNKNOWN").upper() if voice_info else "UNKNOWN"

            avatar_url = f"/api/avatar/{uid}" if has_avatar(uid) else ""

            return {
                "user_id": str(uid),
                "username": username,
                "display_name": display_name,
                "avatar_url": avatar_url,
                "is_in_guild": is_in_guild,
                "voice": {
                    "speaker_id": speaker_id,
                    "speaker_name": speaker_name,
                    "provider": provider,
                    "pitch": st.get("pitch", "0%"),
                    "speed": st.get("speed", 1.0),
                    "volume": st.get("volume", 0.8),
                    "strip_name_brackets": st.get("strip_name_brackets", True),
                    "updated_at": str(st.get("updated_at", ""))
                }
            }

        users_data = [resolve_user_info(st) for st in settings_list]
        return web.json_response({
            "guild_id": str(guild.id),
            "guild_name": guild.name,
            "users": users_data,
            "count": len(users_data)
        })

    async def api_admin_update_guild_user(self, request: web.Request) -> web.Response:
        """
        管理者専用: 指定ユーザーのサーバー別個人設定を更新。
        """
        guild_id_str = request.query.get("guild_id")
        guild, perms = await self._get_guild_and_perms(request, guild_id_str)
        if not (perms["is_admin"] or perms["has_role"]):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Access denied. Only administrators and assigned roles can update user settings."}),
                content_type="application/json"
            )

        user_id_str = request.match_info.get("user_id")
        try:
            target_user_id = int(user_id_str)
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid user_id"}, status=400)

        data = await request.json()
        speaker_id = data.get("speaker_id")
        pitch = data.get("pitch")
        speed = data.get("speed")
        volume = data.get("volume")
        strip_name_brackets = data.get("strip_name_brackets")

        if not speaker_id or speaker_id not in self.config.voices:
            return web.json_response({"error": "Invalid speaker_id"}, status=400)

        try:
            speed_val = float(speed) if speed is not None else 1.0
            if not (0.5 <= speed_val <= 2.0):
                return web.json_response({"error": "Speed must be between 0.5 and 2.0"}, status=400)
        except ValueError:
            return web.json_response({"error": "Invalid speed format"}, status=400)

        volume_val: Optional[float] = None
        if volume is not None:
            try:
                v = float(volume)
                if v > 2.0:
                    v = v / 100.0
                volume_val = max(0.0, min(v, 2.0))
            except (ValueError, TypeError):
                volume_val = 0.8

        strip_brackets_val = bool(strip_name_brackets) if strip_name_brackets is not None else None

        updated = await self.db.update_user_settings(
            user_id=target_user_id,
            speaker_id=speaker_id,
            pitch=str(pitch) if pitch is not None else "0%",
            speed=speed_val,
            volume=volume_val,
            strip_name_brackets=strip_brackets_val,
            guild_id=guild.id
        )

        logger.info(f"Admin updated voice settings for user {target_user_id} in guild {guild.id}: {updated}")
        return web.json_response({
            "success": True,
            "user_id": str(target_user_id),
            "voice": updated
        })

    async def api_admin_delete_guild_user(self, request: web.Request) -> web.Response:
        """
        管理者専用: 指定ユーザーのサーバー別個人設定をリセット（削除）。
        """
        guild_id_str = request.query.get("guild_id")
        guild, perms = await self._get_guild_and_perms(request, guild_id_str)
        if not (perms["is_admin"] or perms["has_role"]):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "Access denied. Only administrators and assigned roles can reset user settings."}),
                content_type="application/json"
            )

        user_id_str = request.match_info.get("user_id")
        try:
            target_user_id = int(user_id_str)
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid user_id"}, status=400)

        deleted = await self.db.delete_guild_user_settings(guild.id, target_user_id)
        logger.info(f"Admin reset voice settings for user {target_user_id} in guild {guild.id} (deleted={deleted})")
        return web.json_response({
            "success": True,
            "user_id": str(target_user_id),
            "deleted": deleted,
            "message": "User settings reset to default"
        })

    def _verify_worker_auth(self, request: web.Request) -> bool:
        """
        Worker 認証検証:
        1. WORKER_SHARED_SECRET 設定時は Bearer / X-Worker-Token / クエリトークンを最優先で照合 (WAN/インターネット経由でも正当なWorkerとして許可)
        2. トークンが有効であれば、WAN/LANにかかわらず承認
        3. トークン未設定、またはリクエストに含まれない場合のみ、ローカルプライベートIPからの直接通信を許可
        """
        secret = getattr(self.config, "worker_shared_secret", "").strip()
        auth_header = request.headers.get("Authorization", "")
        bearer_token = ""
        if auth_header.startswith("Bearer "):
            bearer_token = auth_header[7:].strip()

        token = bearer_token or request.headers.get("X-Worker-Token") or request.query.get("token") or ""
        import hmac
        if secret and token:
            if hmac.compare_digest(token, secret):
                return True
            else:
                return False

        # トークンなし、または未一致の場合: WAN経由は拒否
        if self._is_wan_request(request):
            return False

        # ローカルプライベートIPは互換性のために許可
        peername = request.transport.get_extra_info('peername') if request.transport else None
        client_ip = peername[0] if peername else (request.remote or "")
        if client_ip in ("127.0.0.1", "::1", "localhost") or client_ip.startswith("192.168.") or client_ip.startswith("10."):
            return True

        return False

    async def api_worker_register(self, request: web.Request) -> web.Response:
        """
        Worker からの自動登録リクエストを受け付け、TTSNodePool に動的追加する。
        スペック（GPU/CPU/RAM）に基づいて max_concurrent / priority を自動設定。
        """
        if not self._verify_worker_auth(request):
            return web.json_response({"error": "Unauthorized worker request"}, status=401)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        node_id = data.get("node_id")
        node_name = data.get("node_name", node_id)
        ip = data.get("ip")
        agent_port = data.get("agent_port", 50030)
        rsync_port = data.get("rsync_port", 8730)
        specs = data.get("specs", {})
        engines_data = data.get("engines", {})

        if not node_id or not ip:
            return web.json_response({"error": "node_id and ip are required"}, status=400)

        hostname = specs.get("host_hostname") or specs.get("hostname", node_id)
        # コンテナID (12文字Hex) の露出を防止し、物理ホスト名またはスペックベースへ解決
        clean_host = hostname
        for prefix in ("worker_", "worker-"):
            if clean_host.lower().startswith(prefix):
                clean_host = clean_host[len(prefix):]
        if re.match(r"^[0-9a-fA-F]{12}$", clean_host):
            gpu_name = str(specs.get("gpu_name", "")).strip()
            os_name = str(specs.get("os", "")).strip()
            if gpu_name:
                clean_gpu = gpu_name.replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")
                hostname = f"Worker ({clean_gpu})"
            elif os_name:
                hostname = f"Worker ({os_name})"
            else:
                hostname = f"Worker ({clean_host[:6]})"
        specs["hostname"] = hostname

        mac = specs.get("recommended_max_concurrent", 1)
        pri = specs.get("recommended_priority", 2)

        registered_engines = []
        pool_map = {
            "voicevox": self.tts_manager.voicevox_pool,
            "sharevox": self.tts_manager.sharevox_pool,
            "coeiroink": self.tts_manager.coeiroink_pool,
        }

        for engine_name, engine_info in engines_data.items():
            pool = pool_map.get(engine_name)
            if pool is None:
                continue
            port = engine_info.get("port")
            if not port:
                continue

            from src.config import TTSNodeConfig
            node_config = TTSNodeConfig(
                id=f"{node_id}_{engine_name}",
                url=f"http://{ip}:{port}",
                priority=engine_info.get("priority", pri),
                max_concurrent=engine_info.get("max_concurrent", mac),
                enabled=True,
            )
            eng_running = engine_info.get("running", True)
            node_specs = dict(specs)
            node_specs["engine_running"] = eng_running
            target_node = pool.register_or_update(node_config, specs=node_specs, hostname=hostname)
            target_node.engine_running = eng_running
            if not eng_running:
                target_node.is_healthy = False
            registered_engines.append(engine_name)

        try:
            from src.utils.worker_log_collector import worker_log_collector
            worker_log_collector.register_worker(node_id, ip)
        except Exception:
            pass

        logger.info(
            f"✨ [WorkerAPI] Worker '{node_id}' ({ip}) 登録完了: engines={registered_engines}, "
            f"GPU={'あり' if specs.get('gpu') else 'なし'}, RAM={specs.get('total_ram_gb', '?')}GB, "
            f"concurrent={mac}, priority={pri}"
        )

        # Worker 再接続時: 古いウォームアップキャッシュを無効化し、現在必要なモデルを即時同期
        rm = getattr(self, "resource_manager", None) or (getattr(self.tts_manager, "resource_manager", None) if self.tts_manager else None)
        if rm:
            if hasattr(rm, "invalidate_worker_warmed_models"):
                rm.invalidate_worker_warmed_models(ip)
            if "coeiroink" in registered_engines and hasattr(rm, "sync_desired_ram_models_to_worker"):
                safe_create_task(
                    rm.sync_desired_ram_models_to_worker(ip),
                    name=f"sync_worker_ram_{node_id}"
                )
            if hasattr(rm, "sync_active_vc_models_to_worker"):
                safe_create_task(
                    rm.sync_active_vc_models_to_worker(ip),
                    name=f"sync_worker_active_{node_id}"
                )

        return web.json_response({
            "success": True,
            "node_id": node_id,
            "registered_engines": registered_engines,
            "applied": {"max_concurrent": mac, "priority": pri}
        })

    async def api_worker_heartbeat(self, request: web.Request) -> web.Response:
        """Worker からのハートビートを受け付け、ノード生存状態を更新する"""
        if not self._verify_worker_auth(request):
            return web.json_response({"error": "Unauthorized worker request"}, status=401)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        node_id = data.get("node_id")
        if not node_id:
            return web.json_response({"error": "node_id required"}, status=400)

        pool_map = {
            "voicevox": self.tts_manager.voicevox_pool,
            "sharevox": self.tts_manager.sharevox_pool,
            "coeiroink": self.tts_manager.coeiroink_pool,
        }

        found_any = False
        engines_status = data.get("engines", {})
        resources = data.get("resources", {})
        agent_version = data.get("agent_version", "")

        for engine_name, pool in pool_map.items():
            full_node_id = f"{node_id}_{engine_name}"
            if pool.update_heartbeat(full_node_id):
                found_any = True
                eng_info = engines_status.get(engine_name, {})
                for node in pool.nodes:
                    if node.id == full_node_id:
                        # エンジン稼働状態 & Watchdogヘルス状態
                        eng_running = eng_info.get("running", False)
                        node.engine_running = eng_running
                        node.specs["engine_running"] = eng_running
                        is_eng_healthy = eng_info.get("healthy", True)
                        node.specs["engine_healthy"] = is_eng_healthy
                        # エンジン停止中 (スリープ等) は健全フラグも明示的にFalseにしてリクエスト誤送信を防止
                        if not eng_running:
                            node.is_healthy = False
                        # ワーカー側でハング検知中 (Auto-Heal中) の場合は即座に一時隔離 (スリープ停止中は除外)
                        elif not is_eng_healthy:
                            node.is_healthy = False
                            node.unhealthy_until = time.time() + 20.0
                        # リソース情報 (CPU/メモリ/GPU) を更新
                        if resources:
                            node.specs["resources"] = resources
                            node.specs["resources_updated_at"] = time.time()
                        # Agent バージョン
                        if agent_version:
                            node.specs["agent_version"] = agent_version
                        # RAMDisk 常駐モデル一覧
                        if "ram_models" in data:
                            node.specs["ram_models"] = data["ram_models"]
                        # Agent からの最新スペック (OS詳細名, Dockerバージョン, ホスト名等) をマージ
                        hb_specs = data.get("specs")
                        if isinstance(hb_specs, dict) and hb_specs:
                            for sk, sv in hb_specs.items():
                                if sv not in (None, "", "N/A") or sk not in node.specs:
                                    node.specs[sk] = sv
                        clean_h = node.hostname or ""
                        for prefix in ("worker_", "worker-"):
                            if clean_h.lower().startswith(prefix):
                                clean_h = clean_h[len(prefix):]
                        if re.match(r"^[0-9a-fA-F]{12}$", clean_h):
                            gpu_name = str(node.specs.get("gpu_name", "")).strip()
                            os_name = str(node.specs.get("os", "")).strip()
                            if gpu_name:
                                clean_gpu = gpu_name.replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")
                                node.hostname = f"Worker ({clean_gpu})"
                            elif os_name:
                                node.hostname = f"Worker ({os_name})"
                            else:
                                node.hostname = f"Worker ({clean_h[:6]})"
                            node.specs["hostname"] = node.hostname

        if not found_any:
            return web.json_response({"registered": False, "message": "Node not found. Please re-register."}, status=404)

        # Workerノード別時系列メトリクス履歴を蓄積
        if resources:
            target_node = None
            for p in pool_map.values():
                for n in p.nodes:
                    if n.id.startswith(f"{node_id}_"):
                        target_node = n
                        break
                if target_node:
                    break

            host_k = ""
            if target_node:
                h_name = (target_node.hostname or "").lower()
                host_k = re.sub(r"[^a-zA-Z0-9_-]", "_", h_name).strip("_")
                if not host_k:
                    clean_nid = re.sub(r"[^a-zA-Z0-9_-]", "_", str(node_id)).strip("_")
                    host_k = f"node_{clean_nid}"

            if host_k:
                if host_k not in self.workers_metrics_history:
                    self.workers_metrics_history[host_k] = deque(maxlen=60)
                now_str = datetime.now().strftime("%H:%M:%S")
                point = {
                    "time": now_str,
                    "timestamp": time.time(),
                    "cpu_percent": resources.get("cpu_percent", 0.0),
                    "gpu_util_percent": resources.get("gpu_util_percent", 0.0),
                    "mem_used_gb": resources.get("mem_used_gb", 0.0),
                    "mem_total_gb": resources.get("mem_total_gb", 0.0),
                    "mem_percent": resources.get("mem_percent", 0.0),
                    "gpu_mem_used_mb": resources.get("gpu_mem_used_mb", 0),
                    "gpu_mem_total_mb": resources.get("gpu_mem_total_mb", 0),
                    "gpu_mem_used_gb": round((resources.get("gpu_mem_used_mb") or 0) / 1024, 2),
                    "gpu_mem_total_gb": round((resources.get("gpu_mem_total_mb") or 0) / 1024, 2),
                    "gpu_temp_celsius": resources.get("gpu_temp_celsius", None),
                }
                history_deque = self.workers_metrics_history[host_k]
                if not history_deque or (point["timestamp"] - history_deque[-1].get("timestamp", 0) >= 5):
                    history_deque.append(point)
                self.workers_latest_resources[host_k] = point

        any_human = False
        rm = getattr(self.tts_manager, "resource_manager", None)
        if rm and hasattr(rm, "is_any_human_in_vc"):
            any_human = rm.is_any_human_in_vc()

        # Phase 4: Master主導ピギーバック自動更新 (1台ずつドレイン・更新)
        worker_ip = ""
        peername = request.transport.get_extra_info('peername')
        if peername and isinstance(peername, tuple):
            worker_ip = peername[0]
        if not worker_ip or worker_ip in ("127.0.0.1", "localhost"):
            for p in pool_map.values():
                for n in p.nodes:
                    if n.id.startswith(f"{node_id}_") and getattr(n, "_host", ""):
                        worker_ip = n._host
                        break
                if worker_ip:
                    break

        update_available = False
        target_agent_ver = AGENT_VERSION
        target_manifest_ver = getattr(self, "TARGET_MANIFEST_VERSION", "2026.10.03.1")
        client_manifest = str(data.get("manifest_version") or "").strip()
        need_agent = bool(agent_version) and _version_is_newer(target_agent_ver, agent_version)
        need_full = bool(client_manifest) and client_manifest != target_manifest_ver
        needs_update = need_agent or need_full
        # ハートビート相乗り更新に対応した Agent (v3.12.0 以降) のみが対象。
        # 旧 Agent は update_available を無視するため、許可しても更新されずドレインだけが続いてしまう (従来の毎時チェックに任せる)
        piggyback_capable = bool(agent_version) and not _version_is_newer(self.PIGGYBACK_MIN_AGENT_VERSION, agent_version)

        from src.tts.node_pool import DRAINING_HOSTS
        now = time.time()
        active_piggyback = getattr(self, "_piggyback_updating_host", None)
        piggyback_at = getattr(self, "_piggyback_updating_at", 0.0)
        cooldowns = self._piggyback_cooldown_until

        # タイムアウト超過（15分）の更新ホストがいれば自動解除し、同ホストは15分間再許可しない (Flapping防止)
        if active_piggyback and (now - piggyback_at >= self.PIGGYBACK_UPDATE_TIMEOUT):
            logger.warning(f"⚠️ [Piggyback AutoUpdate] {active_piggyback} の自動更新が15分タイムアウトしたため調停ロックを解除します (15分間は再許可しません)。")
            DRAINING_HOSTS.discard(active_piggyback)
            cooldowns[active_piggyback] = now + self.PIGGYBACK_UPDATE_TIMEOUT
            self._piggyback_updating_host = None
            active_piggyback = None

        if worker_ip and worker_ip not in ("127.0.0.1", "localhost"):
            if not needs_update:
                # このノードが更新完了した（新バージョンで報告してきた）場合、ドレイン解除して次へ
                if active_piggyback == worker_ip:
                    DRAINING_HOSTS.discard(worker_ip)
                    self._piggyback_updating_host = None
                    logger.info(f"✨ [Piggyback AutoUpdate] {worker_ip} の自動更新完了を確認しました (Agent: v{agent_version})。ドレインを解除します。")
            elif active_piggyback == worker_ip:
                # 許可は単発発行 (再発行するとタイムアウトが延長され続けるため何もしない)。完了 or タイムアウトまで待つ
                pass
            elif not piggyback_capable:
                logger.debug(f"[Piggyback AutoUpdate] {worker_ip} は旧Agent(v{agent_version or '?'})のため従来の定期チェックで更新されます。")
            elif now < cooldowns.get(worker_ip, 0.0):
                logger.debug(f"[Piggyback AutoUpdate] {worker_ip} は直近の更新失敗によるクールダウン中です。")
            else:
                # 更新が必要な場合、安全条件をチェック
                rolling_running = bool(getattr(self, "_rolling_state", None) and self._rolling_state.get("running"))
                other_draining = any(h != worker_ip for h in DRAINING_HOSTS)
                other_piggybacking = bool(active_piggyback and active_piggyback != worker_ip)

                if any_human:
                    logger.debug(f"[Piggyback AutoUpdate] {worker_ip} は更新が必要ですが、VC通話中のため保留します。")
                elif rolling_running:
                    logger.debug(f"[Piggyback AutoUpdate] {worker_ip}: WebUIローリング処理実行中のため自動更新を保留します。")
                elif other_draining or other_piggybacking:
                    logger.debug(f"[Piggyback AutoUpdate] {worker_ip}: 別ノードが更新/ドレイン中のため待機させます。")
                else:
                    # このノードに更新許可を発行し、ドレインを開始
                    DRAINING_HOSTS.add(worker_ip)
                    self._piggyback_updating_host = worker_ip
                    self._piggyback_updating_at = now
                    update_available = True
                    logger.info(
                        f"🚀 [Piggyback AutoUpdate] {worker_ip} に更新許可を発行 (Agent: {agent_version} → {target_agent_ver}, "
                        f"Manifest: {client_manifest or '-'} → {target_manifest_ver})。ノードをドレインします。"
                    )

        return web.json_response({
            "registered": True,
            "node_id": node_id,
            "any_human_in_vc": any_human,
            "update_available": update_available,
            "target_agent_version": target_agent_ver,
            "target_manifest_version": target_manifest_ver,
            "require_full": need_full,
        })

    async def api_worker_nodes(self, request: web.Request) -> web.Response:
        """全 Worker ノードの状態一覧を返す（WebUI / 管理用）"""
        user = self.auth.get_current_user(request)
        if not user:
            return web.json_response({"error": "Unauthorized"}, status=401)

        pool_map = {
            "voicevox": self.tts_manager.voicevox_pool,
            "sharevox": self.tts_manager.sharevox_pool,
            "coeiroink": self.tts_manager.coeiroink_pool,
        }
        result = {}
        for engine_name, pool in pool_map.items():
            result[engine_name] = pool.get_status()

        return web.json_response({"pools": result})

    async def api_worker_kick_node(self, request: web.Request) -> web.Response:
        """指定した Worker ノードの切断・キック・除外 (管理者のみ)"""
        await self._require_admin(request)
        try:
            data = await request.json()
        except Exception:
            data = {}

        node_id = str(data.get("node_id", "")).strip()
        ip = str(data.get("ip", "")).strip()
        if not node_id and not ip:
            return web.json_response({"error": "node_id or ip is required"}, status=400)

        kicked = False
        target_name = node_id or ip

        # 1. ボランティア Worker (WebSocket) の切断
        for cid, conn in list(self.worker_ws_connections.items()):
            conn_node_id = getattr(conn, "node_id", None) or cid
            conn_ip = getattr(conn, "remote_ip", None) or ""
            if (node_id and (conn_node_id == node_id or cid == node_id)) or (ip and conn_ip == ip):
                try:
                    ws = getattr(conn, "ws", conn)
                    if hasattr(ws, "close") and not ws.closed:
                        await ws.close(code=4000, message=b"Kicked by administrator")
                    kicked = True
                    logger.info(f"🔌 [Worker Kick] Volunteer Worker {cid} (node: {conn_node_id}) was kicked by admin.")
                except Exception as e:
                    logger.warning(f"Error kicking WS connection {cid}: {e}")

        # 2. 常設 Worker Pool からの無効化 / 除外
        pool_map = {
            "voicevox": self.tts_manager.voicevox_pool,
            "sharevox": self.tts_manager.sharevox_pool,
            "coeiroink": self.tts_manager.coeiroink_pool,
        }
        for engine_name, pool in pool_map.items():
            if hasattr(pool, "nodes"):
                for n in list(pool.nodes):
                    nid = getattr(n, "id", "")
                    nurl = getattr(n, "url", "")
                    if (node_id and (nid == node_id or nurl == node_id)) or (ip and (ip in nurl or getattr(n, "host", "") == ip)):
                        if hasattr(n, "is_active"):
                            n.is_active = False
                        if hasattr(pool, "remove_node"):
                            pool.remove_node(n.id)
                        kicked = True
                        logger.info(f"🚫 [Worker Kick] 常設 Worker {nid} ({nurl}) in {engine_name} was removed/deactivated.")

        return web.json_response({
            "status": "ok",
            "kicked": kicked,
            "target": target_name,
            "message": f"Worker ノード '{target_name}' を切断・除外しました。"
        })

    async def api_worker_broadcast_update(self, request: web.Request) -> web.Response:
        """接続中の全 Volunteer Worker ノードへ自己アップデート命令を一斉ブロードキャスト (管理者専用)"""
        await self._require_admin(request)
        count = 0
        failed = 0
        for node_id, ws in list(self.worker_ws_connections.items()):
            try:
                if hasattr(ws, "send_json") and not ws.closed:
                    await ws.send_json({
                        "type": "update",
                        "action": "update",
                        "timestamp": time.time(),
                        "message": "Update triggered by administrator"
                    })
                    count += 1
            except Exception as e:
                logger.warning(f"Failed to send update signal to volunteer worker {node_id}: {e}")
                failed += 1
        logger.info(f"📢 [Worker Update] Broadcasted update signal to {count} volunteer worker(s) (failed: {failed})")
        return web.json_response({"status": "ok", "broadcast_count": count, "failed_count": failed})

    async def api_worker_trigger_update(self, request: web.Request) -> web.Response:
        """指定ノードまたは全ノードの Worker Agent に自己アップデート・完全再構成を指示する (管理者権限必須)"""
        await self._require_admin(request)

        try:
            data = await request.json()
        except Exception:
            data = {}

        target_all = bool(data.get("target_all", False))
        full = bool(data.get("full", True))
        update_agent = bool(data.get("update_agent", True))
        update_engines = bool(data.get("update_engines", True))

        # 全ノード一括更新は全台同時ダウンを避けるためローリング更新 (1台ずつ) に振り替える
        if target_all:
            return self._start_rolling_job("update", full=full)

        payload = {
            "full": full,
            "update_agent": update_agent,
            "update_engines": update_engines
        }

        # ターゲットノード一覧の解決
        targets = []
        if target_all:
            seen_ips = set()
            for pool in [self.tts_manager.voicevox_pool, self.tts_manager.sharevox_pool, self.tts_manager.coeiroink_pool]:
                for node in pool.nodes:
                    from urllib.parse import urlparse
                    parsed = urlparse(node.url)
                    ip = parsed.hostname
                    port = getattr(node, "agent_port", 50030) or 50030
                    key = (ip, port)
                    if ip and key not in seen_ips and ip != "localhost" and ip != "127.0.0.1":
                        seen_ips.add(key)
                        targets.append({"ip": ip, "agent_port": port, "node_id": node.id})
        else:
            req_ip = str(data.get("ip") or "").strip()
            if not req_ip:
                return web.json_response({"error": "ip is required when target_all is false"}, status=400)

            # サーバー側で管理している既知のワーカーから一致するものをルックアップ (CodeQL SSRF 防護 / 外部入力Taint切断)
            matched_worker = None
            for host_info in self._collect_worker_hosts():
                if host_info.get("ip") == req_ip:
                    matched_worker = host_info
                    break

            if not matched_worker and hasattr(self, "worker_log_collector"):
                for k_id, k_ip in list(self.worker_log_collector._known_workers.items()):
                    if k_ip == req_ip or k_id == req_ip:
                        matched_worker = {"ip": k_ip, "agent_port": 50030, "node_id": k_id}
                        break

            if not matched_worker:
                if not validate_safe_worker_ip(req_ip):
                    return web.json_response({"error": "Invalid or disallowed worker IP address (SSRF guard)"}, status=400)
                return web.json_response({"error": "Worker not registered or recognized"}, status=404)

            safe_ip = matched_worker["ip"]
            safe_port = int(matched_worker.get("agent_port", 50030))
            targets.append({"ip": safe_ip, "agent_port": safe_port, "node_id": matched_worker.get("node_id", "single")})

        if not targets:
            return web.json_response({"success": False, "error": "更新対象のワーカーノードが見つかりません。"}, status=404)

        import aiohttp
        async def _notify_worker(t: Dict[str, Any]) -> Dict[str, Any]:
            ip = t["ip"]
            if not validate_safe_worker_ip(ip):
                return {"ip": ip, "success": False, "error": "Disallowed or invalid worker IP address"}
            port = int(t.get("agent_port", 50030))
            if not (1 <= port <= 65535):
                return {"ip": ip, "success": False, "error": "Invalid port number"}
            update_url = f"http://{ip}:{port}/update"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(update_url, json=payload, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
                        body = await resp.json()
                        return {"ip": ip, "success": True, "worker_response": body}
            except Exception as e:
                return {"ip": ip, "success": False, "error": str(e)}

        results = await asyncio.gather(*[_notify_worker(t) for t in targets])
        all_ok = any(r["success"] for r in results)
        return web.json_response({
            "success": all_ok,
            "target_count": len(targets),
            "results": results,
            "payload": payload
        })

    # ===== ローリング更新 / 再起動 (1台ずつ・ドレイン付き) =====
    ROLLING_UPDATE_NODE_TIMEOUT = 900.0   # full 再構成は Worker 側ビルドが最大600秒かかるため余裕を持たせる
    ROLLING_RESTART_NODE_TIMEOUT = 180.0
    ROLLING_ENGINE_RECOVER_TIMEOUT = 180.0

    def _worker_pools(self) -> List[Any]:
        return [self.tts_manager.voicevox_pool, self.tts_manager.sharevox_pool, self.tts_manager.coeiroink_pool]

    def _nodes_for_host(self, ip: str) -> List[Any]:
        """ホストIPに属する全エンジンノード (再登録でオブジェクトが作り直されても毎回引き直す)"""
        return [n for p in self._worker_pools() for n in p.nodes if getattr(n, "_host", "") == ip]

    def _collect_worker_hosts(self, only_ip: Optional[str] = None) -> List[Dict[str, Any]]:
        hosts: Dict[str, Dict[str, Any]] = {}
        for pool in self._worker_pools():
            for node in pool.nodes:
                ip = getattr(node, "_host", "")
                if not ip or ip in ("localhost", "127.0.0.1") or (only_ip and ip != only_ip):
                    continue
                h = hosts.setdefault(ip, {
                    "ip": ip,
                    "agent_port": getattr(node, "agent_port", 50030) or 50030,
                    "label": node.hostname or ip,
                    "has_gpu": False,
                })
                h["has_gpu"] = h["has_gpu"] or bool(getattr(node, "has_gpu", False))
        # CPUノードを先に (カナリア)、GPUノードを後に更新する
        return sorted(hosts.values(), key=lambda h: (h["has_gpu"], h["label"]))

    @staticmethod
    async def _agent_health(session, base: str) -> Optional[Dict[str, Any]]:
        import aiohttp
        try:
            async with session.get(f"{base}/health", timeout=aiohttp.ClientTimeout(total=4.0)) as r:
                if r.status == 200:
                    return await r.json()
        except Exception:
            pass
        return None

    async def _wait_agent_settled(self, session, base: str, before: Optional[Dict[str, Any]],
                                  timeout: float, action: str, state: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Agent の処理完了を待つ。
        - 新Agent (/health に updating あり): 「処理中 or ダウン or プロセス入替」を観測後、updating=False が一定時間安定したら完了
        - 旧Agent (updating なし): ダウンを観測するか 90秒経過で完了と推定
        """
        start = time.time()
        saw_busy = False
        last_busy = start
        stable_needed = 20.0 if action == "update" else 10.0
        await asyncio.sleep(5)
        while time.time() - start < timeout:
            h = await self._agent_health(session, base)
            now = time.time()
            elapsed = int(now - start)
            if h is None:
                saw_busy, last_busy = True, now
                state["current_detail"] = f"Agent 再起動待ち ({elapsed}s)"
            elif "updating" in h:
                restarted = bool(before and h.get("started_at") and h.get("started_at") != before.get("started_at"))
                if h.get("updating"):
                    saw_busy, last_busy = True, now
                    state["current_detail"] = f"{'更新' if action == 'update' else '再起動'}処理中 ({elapsed}s)"
                elif (saw_busy or restarted) and now - last_busy >= stable_needed:
                    ver = h.get("agent_version", "?")
                    note = "" if action != "update" or ver == AGENT_VERSION else f" ※最新 v{AGENT_VERSION} と不一致"
                    return True, f"完了 (Agent v{ver}){note}"
                elif action == "update" and not saw_busy and now - start >= 30:
                    return True, f"更新対象なしで終了 (Agent v{h.get('agent_version', '?')})"
                else:
                    state["current_detail"] = f"安定待ち ({elapsed}s)"
            else:
                if saw_busy or now - start >= 90:
                    return True, "完了 (旧Agentのため完了を推定)"
                state["current_detail"] = f"旧Agent 完了推定待ち ({elapsed}s)"
            await asyncio.sleep(5)
        return False, f"{int(timeout)}秒以内に Agent の処理完了を確認できませんでした"

    async def _wait_engines_healthy(self, ip: str, timeout: float) -> bool:
        """更新前に稼働していたエンジンが、ハートビート上で稼働かつ健全に戻るまで待つ"""
        start = time.time()
        while time.time() - start < timeout:
            nodes = self._nodes_for_host(ip)
            running = [n for n in nodes if n.specs.get("engine_running")]
            fresh = all(time.time() - n.last_heartbeat < 40 for n in nodes) if nodes else False
            if running and fresh and all(n.specs.get("engine_healthy", True) for n in running):
                return True
            await asyncio.sleep(5)
        return False

    async def _rolling_process_host(self, host: Dict[str, Any], action: str, full: bool,
                                    state: Dict[str, Any]) -> Tuple[bool, str]:
        import aiohttp
        from src.tts.node_pool import DRAINING_HOSTS
        ip, base = host["ip"], f"http://{host['ip']}:{host['agent_port']}"
        DRAINING_HOSTS.add(ip)
        logger.info(f"🔁 [Rolling {action}] {host['label']} ({ip}) をドレインして処理を開始します。")
        try:
            # 1. 実行中の推論が捌けるまで待機 (最大30秒)
            state["current_detail"] = "実行中の推論の完了待ち"
            t0 = time.time()
            while time.time() - t0 < 30 and any(n.active_count > 0 for n in self._nodes_for_host(ip)):
                await asyncio.sleep(1)
            engines_were_running = any(n.specs.get("engine_running") for n in self._nodes_for_host(ip))

            async with aiohttp.ClientSession() as session:
                before = await self._agent_health(session, base)
                if before is None:
                    return False, "Agent に接続できません"
                if action == "restart":
                    async with session.post(f"{base}/restart", timeout=aiohttp.ClientTimeout(total=8.0)) as r:
                        if r.status == 404:
                            return False, "Agent が旧バージョンのため再起動APIに未対応です (v3.12.0 以降で利用可能)"
                        if r.status not in (200, 409):
                            return False, f"再起動指示に失敗しました (HTTP {r.status})"
                    timeout = self.ROLLING_RESTART_NODE_TIMEOUT
                else:
                    payload = {"full": full, "update_agent": True, "update_engines": True}
                    async with session.post(f"{base}/update", json=payload, timeout=aiohttp.ClientTimeout(total=8.0)) as r:
                        if r.status not in (200, 409):  # 409 = 既に更新中 → 完了待ちへ
                            return False, f"更新指示に失敗しました (HTTP {r.status})"
                    timeout = self.ROLLING_UPDATE_NODE_TIMEOUT
                ok, msg = await self._wait_agent_settled(session, base, before, timeout, action, state)
            if not ok:
                return False, msg

            # 2. 更新前に稼働していたエンジンのみ回復を確認 (Vacancy Sleep で停止中なら確認不要)
            if engines_were_running:
                state["current_detail"] = "エンジンのヘルス回復待ち"
                if not await self._wait_engines_healthy(ip, self.ROLLING_ENGINE_RECOVER_TIMEOUT):
                    return False, f"Agent は復帰しましたが、エンジンが{int(self.ROLLING_ENGINE_RECOVER_TIMEOUT)}秒以内に健全化しませんでした"
            return True, msg
        except Exception as e:
            return False, f"例外: {e}"
        finally:
            DRAINING_HOSTS.discard(ip)

    async def _run_rolling_job(self, action: str, full: bool, only_ip: Optional[str]) -> None:
        st = self._rolling_state
        try:
            hosts = self._collect_worker_hosts(only_ip)
            st.update({"total": len(hosts)})
            for i, h in enumerate(hosts):
                st["current"] = h["label"]
                st["current_ip"] = h["ip"]
                nodes = self._nodes_for_host(h["ip"])
                if nodes and all(time.time() - n.last_heartbeat > 90 for n in nodes):
                    st["results"].append({"label": h["label"], "ip": h["ip"], "success": True, "skipped": True,
                                          "message": "オフラインのためスキップ"})
                    st["done"] = i + 1
                    continue
                ok, msg = await self._rolling_process_host(h, action, full, st)
                st["results"].append({"label": h["label"], "ip": h["ip"], "success": ok, "message": msg})
                logger.info(f"🔁 [Rolling {action}] {h['label']} ({h['ip']}): {'✅' if ok else '❌'} {msg}")
                if not ok:
                    st["error"] = f"{h['label']}: {msg}"
                    logger.error(f"❌ [Rolling {action}] {h['label']} で失敗したため、残りのノードの処理を中断しました。")
                    break
                st["done"] = i + 1
                if i < len(hosts) - 1:
                    st["current_detail"] = "次のノードへ移行前のクールダウン"
                    await asyncio.sleep(5)
        except Exception as e:
            st["error"] = f"ジョブ例外: {e}"
            logger.error(f"❌ [Rolling {action}] ジョブ例外: {e}")
        finally:
            st.update({"running": False, "current": None, "current_ip": None, "current_detail": "", "finished_at": time.time()})

    def _start_rolling_job(self, action: str, full: bool = True, only_ip: Optional[str] = None) -> web.Response:
        st = getattr(self, "_rolling_state", None)
        if st and st.get("running"):
            return web.json_response({"success": False, "error": "別のローリング処理が実行中です。", "state": st}, status=409)
        hosts = self._collect_worker_hosts(only_ip)
        if not hosts:
            return web.json_response({"success": False, "error": "対象のワーカーノードが見つかりません。"}, status=404)
        self._rolling_state = {
            "running": True, "action": action, "full": full, "total": len(hosts), "done": 0,
            "current": None, "current_ip": None, "current_detail": "", "results": [], "error": None,
            "started_at": time.time(), "finished_at": None,
        }
        self._rolling_task = safe_create_task(self._run_rolling_job(action, full, only_ip), name=f"rolling_{action}")
        return web.json_response({"success": True, "rolling": True, "target_count": len(hosts),
                                  "order": [h["label"] for h in hosts], "state": self._rolling_state})

    async def api_worker_rolling_start(self, request: web.Request) -> web.Response:
        """全ワーカーのローリング更新 / ローリング再起動を開始 (管理者権限必須)"""
        await self._require_admin(request)
        try:
            data = await request.json()
        except Exception:
            data = {}
        action = data.get("action", "update")
        if action not in ("update", "restart"):
            return web.json_response({"error": "action must be 'update' or 'restart'"}, status=400)
        return self._start_rolling_job(action, full=bool(data.get("full", True)))

    async def api_worker_rolling_status(self, request: web.Request) -> web.Response:
        if not self.auth.get_current_user(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return web.json_response({"state": getattr(self, "_rolling_state", None) or {"running": False}})

    async def api_worker_restart(self, request: web.Request) -> web.Response:
        """指定ワーカー1台のエンジン＋Agent再起動 (管理者権限必須)"""
        await self._require_admin(request)
        try:
            data = await request.json()
        except Exception:
            data = {}
        ip = str(data.get("ip") or "").strip()
        if not ip:
            return web.json_response({"error": "ip is required"}, status=400)
        return self._start_rolling_job("restart", full=False, only_ip=ip)

    async def api_worker_logs(self, request: web.Request) -> web.Response:
        """指定 Worker または全 Worker のログを取得して返却 (ローカルファイルキャッシュ & オンデマンド取得対応)"""
        peername = request.transport.get_extra_info('peername')
        client_ip = peername[0] if peername else ""
        is_local = (
            client_ip in ("127.0.0.1", "::1", "localhost") or
            client_ip.startswith("172.") or
            client_ip.startswith("10.") or
            client_ip.startswith("192.168.")
        )
        if not is_local:
            user = self.auth.get_current_user(request)
            if not user:
                return web.json_response({"error": "Unauthorized"}, status=401)

        from src.utils.worker_log_collector import worker_log_collector

        node_id = request.query.get("node_id", "").strip()
        target = request.query.get("target", "agent").lower()
        lines = int(request.query.get("lines", request.query.get("tail", "100")))
        refresh = request.query.get("refresh", "0") in ("1", "true")
        mode = request.query.get("mode", "")

        # 1. ワーカー一覧・ファイル一覧の要求
        if mode == "list" or not node_id:
            available = worker_log_collector.list_available_worker_logs()
            all_known = list(worker_log_collector._known_workers.keys())
            for nid in all_known:
                if not any(w["node_id"] == nid for w in available):
                    available.append({
                        "node_id": nid,
                        "dir_path": "",
                        "files": []
                    })
            return web.json_response({
                "success": True,
                "workers": available
            })

        # 2. 既知ワーカーノードから安全なIPをルックアップ (外部入力Taint切断 / CodeQL SSRF 防護)
        safe_target_ip = None
        req_ip = str(request.query.get("ip", "")).strip()

        # まず node_id で検索
        if node_id and node_id in worker_log_collector._known_workers:
            safe_target_ip = worker_log_collector._known_workers[node_id]

        # req_ip が指定されている場合、既知のワーカー一覧から一致するサーバー管理のIPを取得
        if not safe_target_ip and req_ip:
            for known_id, known_ip in list(worker_log_collector._known_workers.items()):
                if known_ip == req_ip or known_id == req_ip:
                    safe_target_ip = known_ip
                    break
            if not safe_target_ip:
                for h in self._collect_worker_hosts():
                    if h.get("ip") == req_ip:
                        safe_target_ip = h["ip"]
                        break

        if refresh and safe_target_ip:
            await worker_log_collector.collect_single_worker(node_id, safe_target_ip, tail=max(lines, 200))

        # 3. 保存済みファイルからログを取得
        content = worker_log_collector.get_log_content(node_id, target=target, tail=lines)
        if content.get("success"):
            return web.json_response(content)

        # ファイルが未生成の場合は、直接ワーカーへHTTPフェッチを試みる (フォールバック)
        if safe_target_ip and validate_safe_worker_ip(safe_target_ip):
            try:
                import aiohttp
                safe_target = sanitize_filename(str(target))
                if not safe_target:
                    safe_target = "agent"
                safe_lines = max(1, min(int(lines), 1000))
                url = f"http://{safe_target_ip}:50030/logs?target={safe_target}&lines={safe_lines}"
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3.5)) as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            await worker_log_collector.collect_single_worker(node_id, safe_target_ip, tail=lines)
                            raw_lines = data.get("logs") or data.get("agent_logs") or []
                            return web.json_response({
                                "success": True,
                                "node_id": node_id,
                                "target": target,
                                "lines": raw_lines[-lines:],
                                "total_lines": len(raw_lines)
                            })
            except Exception as e:
                return web.json_response({"success": False, "node_id": node_id, "error": str(e)}, status=500)

        return web.json_response(content)

    async def api_worker_manifest(self, request: web.Request) -> web.Response:
        """
        Worker ノード向け構成マニフェスト (Single Source of Truth) を返却。
        各 Worker は本 API を参照して Compose や Dockerfile を自律レンダリング・最新化する。
        """
        # 一時的な旧Worker再起動トリガー (Python 3.11 クライアントのみ対象)
        user_agent = request.headers.get("User-Agent", "")
        trigger_file = "/app/data/.trigger_worker_reboot"
        if "Python/3.11" in user_agent and os.path.exists(trigger_file):
            logger.info(f"🔄 [Worker Migration] Python 3.11 Worker ({user_agent}) への再起動シグナルを送信中...")
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "application/json"}
            )
            await resp.prepare(request)
            chunk = b" " * (1024 * 1024)
            for _ in range(280):
                await resp.write(chunk)
            await resp.write_eof()
            return resp

        manifest = {
            "manifest_version": self.TARGET_MANIFEST_VERSION,
            "agent_version": AGENT_VERSION,
            "engines": {
                "voicevox": {
                    "image_gpu": "voicevox/voicevox_engine:nvidia-ubuntu20.04-latest",
                    "image_cpu": "voicevox/voicevox_engine:cpu-ubuntu20.04-latest",
                    "port": 50021,
                    "health_path": "/version"
                },
                "sharevox": {
                    "port": 50025,
                    "health_path": "/version",
                    "dockerfile_gpu": (
                        "FROM ubuntu:20.04 AS downloader\n"
                        "ENV DEBIAN_FRONTEND=noninteractive\n"
                        "RUN apt-get update && apt-get install -y curl tar ca-certificates && rm -rf /var/lib/apt/lists/*\n"
                        "WORKDIR /tmp/sv\n"
                        "RUN curl -sL \"https://github.com/SHAREVOX/sharevox/releases/download/0.2.1/sharevox-linux-cpu-0.2.1.tar.gz\" | tar -xzf - --strip-components=1\n\n"
                        "FROM sharevox/sharevox_engine:nvidia-ubuntu20.04-latest\n"
                        "USER root\n"
                        "RUN mkdir -p /opt/sharevox_engine/library_info\n"
                        "COPY --from=downloader /tmp/sv/model /opt/sharevox_engine/model\n"
                        "COPY --from=downloader /tmp/sv/speaker_info /opt/sharevox_engine/speaker_info\n"
                        "EXPOSE 50025\n"
                        "ENTRYPOINT [\"/entrypoint.sh\"]\n"
                        "CMD [\"gosu\", \"user\", \"/opt/python/bin/python3\", \"./run.py\", \"--use_gpu\", \"--voicelib_dir\", \"/opt/sharevox_core/\", \"--runtime_dir\", \"/opt/onnxruntime/lib\", \"--host\", \"0.0.0.0\", \"--port\", \"50025\"]\n"
                    ),
                    "dockerfile_cpu": (
                        "FROM ubuntu:20.04\n"
                        "ENV DEBIAN_FRONTEND=noninteractive\n"
                        "RUN apt-get update && apt-get install -y curl tar ca-certificates libgomp1 libsndfile1 && rm -rf /var/lib/apt/lists/*\n"
                        "WORKDIR /opt/sharevox\n"
                        "RUN curl -sL \"https://github.com/SHAREVOX/sharevox/releases/download/0.2.1/sharevox-linux-cpu-0.2.1.tar.gz\" | tar -xzf - --strip-components=1 && chmod +x /opt/sharevox/run\n"
                        "EXPOSE 50025\n"
                        "ENTRYPOINT [\"/opt/sharevox/run\"]\n"
                        "CMD [\"--host\", \"0.0.0.0\", \"--port\", \"50025\", \"--cpu_num_threads\", \"{THREAD_NUM}\"]\n"
                    )
                },
                "coeiroink": {
                    "port": 50031,
                    "health_path": "/docs",
                    "url_gpu": "https://www.dropbox.com/scl/fi/pisvh3pwlinz6hdep2fcr/COEIROINK_LINUX_GPU_v.2025.4.1.zip?rlkey=4dg1368zzdbplahcjdbqoj2tg&st=3k7yonjs&dl=1",
                    "url_cpu": "https://www.dropbox.com/scl/fi/o7xnjfo0jw81otgn2jxar/COEIROINK_LINUX_CPU_v.2025.4.1.zip?rlkey=dt1z2bxrbo3ikm2iiioxh9wx7&st=tso7516f&dl=1",
                    "dir_gpu": "COEIROINK_LINUX_GPU_v.2025.4.1",
                    "dir_cpu": "COEIROINK_LINUX_CPU_v.2025.4.1",
                    "base_image_gpu": "nvidia/cuda:11.8.0-base-ubuntu22.04",
                    "base_image_cpu": "ubuntu:22.04",
                    "dockerfile_template": (
                        "FROM {BASE_IMAGE}\n"
                        "ENV DEBIAN_FRONTEND=noninteractive\n"
                        "RUN apt-get update && apt-get install -y curl unzip ca-certificates libgomp1 libsndfile1 socat && rm -rf /var/lib/apt/lists/*\n"
                        "WORKDIR /opt/coeiroink\n"
                        "RUN curl -L -o /tmp/coeiroink.zip \"{COEIROINK_ZIP_URL}\" && \\\n"
                        "    unzip -q /tmp/coeiroink.zip -d /opt/coeiroink && \\\n"
                        "    rm /tmp/coeiroink.zip && \\\n"
                        "    chmod +x /opt/coeiroink/{COEIROINK_DIR}/engine/engine\n"
                        "COPY entrypoint.sh /entrypoint.sh\n"
                        "RUN chmod +x /entrypoint.sh\n"
                        "EXPOSE 50031 50032\n"
                        "ENTRYPOINT [\"/entrypoint.sh\"]\n"
                    ),
                    "entrypoint_sh": (
                        "#!/bin/bash\n"
                        "set -e\n"
                        "DIR=$(ls -d /opt/coeiroink/COEIROINK_LINUX_* | head -n 1)\n"
                        "echo \"Found COEIROINK directory: $DIR\"\n"
                        "cd \"$DIR/engine\"\n"
                        "export LD_LIBRARY_PATH=\"$DIR/engine:$DIR/engine/torch/lib:/usr/local/cuda/lib64:$LD_LIBRARY_PATH\"\n"
                        "socat TCP-LISTEN:50031,fork,reuseaddr TCP:127.0.0.1:50032 &\n"
                        "SOCAT_PID=$!\n"
                        "\"$DIR/engine/engine\" &\n"
                        "ENGINE_PID=$!\n"
                        "wait -n $ENGINE_PID $SOCAT_PID\n"
                    )
                }
            },
            "compose_template": (
                "# ==============================================================================\n"
                "# TTSBot Compute Worker - 自動生成 Compose ファイル\n"
                "# ==============================================================================\n\n"
                "name: ttsbot-worker-engines\n\n"
                "services:\n"
                "  worker-voicevox:\n"
                "    image: {VOICEVOX_IMAGE}\n"
                "    container_name: ttsworker-voicevox\n"
                "    restart: unless-stopped\n"
                "    command: [\"gosu\", \"user\", \"/opt/python/bin/python3\", \"./run.py\", \"--voicelib_dir\", \"/opt/voicevox_core/\", \"--runtime_dir\", \"/opt/onnxruntime/lib\", \"--host\", \"0.0.0.0\", \"--cpu_num_threads\", \"{THREAD_NUM}\"]\n"
                "    ports:\n"
                "      - \"50021:50021\"\n"
                "    environment:\n"
                "      - TZ=Asia/Tokyo\n"
                "      - OMP_NUM_THREADS={THREAD_NUM}\n"
                "      - MKL_NUM_THREADS={THREAD_NUM}\n"
                "{VV_DEPLOY}\n"
                "    logging:\n"
                "      driver: \"json-file\"\n"
                "      options:\n"
                "        max-size: \"10m\"\n"
                "        max-file: \"2\"\n\n"
                "  worker-sharevox:\n"
                "    build:\n"
                "      context: ./docker/sharevox\n"
                "      dockerfile: Dockerfile\n"
                "    container_name: ttsworker-sharevox\n"
                "    restart: unless-stopped\n"
                "    ports:\n"
                "      - \"50025:50025\"\n"
                "    environment:\n"
                "      - TZ=Asia/Tokyo\n"
                "      - OMP_NUM_THREADS={THREAD_NUM}\n"
                "      - MKL_NUM_THREADS={THREAD_NUM}\n"
                "{SV_DEPLOY}\n"
                "    logging:\n"
                "      driver: \"json-file\"\n"
                "      options:\n"
                "        max-size: \"10m\"\n"
                "        max-file: \"2\"\n\n"
                "  worker-coeiroink:\n"
                "    build:\n"
                "      context: ./docker/coeiroink\n"
                "      dockerfile: Dockerfile\n"
                "    container_name: ttsworker-coeiroink\n"
                "    restart: unless-stopped\n"
                "    ports:\n"
                "      - \"50031:50031\"\n"
                "    environment:\n"
                "      - TZ=Asia/Tokyo\n"
                "      - OMP_NUM_THREADS={THREAD_NUM}\n"
                "      - MKL_NUM_THREADS={THREAD_NUM}\n"
                "      - TORCH_NUM_THREADS={THREAD_NUM}\n"
                "      - OPENBLAS_NUM_THREADS={THREAD_NUM}\n"
                "    volumes:\n"
                "      - ./coeiroink_hybrid:/opt/coeiroink/{COEIROINK_DIR}/speaker_info\n"
                "      - ./coeiroink_models:/opt/coeiroink/{COEIROINK_DIR}/storage/ssd:ro\n"
                "      - ./coeiroink_ram:/opt/coeiroink/{COEIROINK_DIR}/storage/ram\n"
                "{CI_DEPLOY}\n"
                "    logging:\n"
                "      driver: \"json-file\"\n"
                "      options:\n"
                "        max-size: \"10m\"\n"
                "        max-file: \"2\"\n"
            )
        }
        return web.json_response(manifest)

    async def api_worker_models_list(self, request: web.Request) -> web.Response:
        """Worker 向け COEIROINK モデル一覧 (モデルフォルダ名, 表示名, UUID, スタイル数, 推定サイズ, tree_sha256) を返却"""
        model_dir = Path(os.getenv("COEIROINK_MODEL_DIR", "/app/coeiroink_models"))
        if not model_dir.exists():
            return web.json_response({"models": [], "total_count": 0})

        from src.utils.model_manifest import build_all_models_summary
        summaries = await asyncio.to_thread(build_all_models_summary, model_dir)
        models = []
        for s in summaries:
            models.append({
                "folder_name": s["model_name"],
                "model_name": s["model_name"],
                "speaker_name": s["speaker_name"],
                "speaker_uuid": s["speaker_uuid"],
                "style_count": s["style_count"],
                "total_files": s["total_files"],
                "size_bytes": s["total_bytes"],
                "size_mb": s["size_mb"],
                "tree_sha256": s["tree_sha256"]
            })
        return web.json_response({"models": models, "total_count": len(models)})

    async def api_worker_models_manifest(self, request: web.Request) -> web.Response:
        """指定モデルの詳細ファイルマニフェスト（ファイル一覧・各SHA256ハッシュ・サイズ・ツリーハッシュ）を返却"""
        model_name = request.match_info.get("model_name", "")
        clean_model_name = sanitize_filename(model_name)
        if not clean_model_name or not is_safe_identifier(clean_model_name):
            return web.Response(text="Invalid model name", status=400)

        model_dir = Path(os.getenv("COEIROINK_MODEL_DIR", "/app/coeiroink_models")).resolve()
        target_path = validate_safe_path(model_dir, clean_model_name)
        if not target_path or not target_path.exists() or not target_path.is_dir() or not (target_path / "metas.json").exists():
            return web.Response(text="Model not found", status=404)

        from src.utils.model_manifest import build_model_manifest
        manifest = await asyncio.to_thread(build_model_manifest, target_path)
        if not manifest:
            return web.Response(text="Failed to build manifest", status=500)
        return web.json_response(manifest)

    async def api_worker_models_file(self, request: web.Request) -> web.StreamResponse:
        """モデル内の指定単一ファイルをストリーミング配信（HTTP Range レジューム対応・パストラバーサル防止）"""
        model_name = request.match_info.get("model_name", "")
        file_path_str = request.match_info.get("file_path", "")

        clean_model_name = sanitize_filename(model_name)
        if not clean_model_name or not is_safe_identifier(clean_model_name):
            return web.Response(text="Invalid model name", status=400)

        # パスセグメントを個別にサニタイズ (CWE-22 防護)
        segments = [sanitize_filename(s) for s in file_path_str.replace("\\", "/").split("/") if s]
        if not segments or any(not s for s in segments):
            return web.Response(text="Invalid file path", status=400)
        clean_relative_path = "/".join(segments)

        model_dir = Path(os.getenv("COEIROINK_MODEL_DIR", "/app/coeiroink_models")).resolve()
        target_model_dir = validate_safe_path(model_dir, clean_model_name)
        if not target_model_dir or not target_model_dir.exists() or not target_model_dir.is_dir():
            return web.Response(text="Model not found", status=404)

        # パストラバーサル防止 (CodeQL CWE-22)
        target_file = validate_safe_path(target_model_dir, clean_relative_path)
        if not target_file or not target_file.exists() or not target_file.is_file():
            return web.Response(text="File not found", status=404)

        file_size = target_file.stat().st_size
        range_header = request.headers.get("Range")

        # Range リクエスト処理 (bytes=start-end)
        if range_header and range_header.startswith("bytes="):
            try:
                range_val = range_header[6:].strip()
                start_str, end_str = range_val.split("-", 1)
                start = int(start_str) if start_str else 0
                end = int(end_str) if end_str else file_size - 1
                if start < 0 or start >= file_size or end >= file_size or start > end:
                    return web.Response(
                        status=416,
                        headers={"Content-Range": f"bytes */{file_size}"}
                    )
                content_length = end - start + 1
                response = web.StreamResponse(
                    status=206,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"bytes {start}-{end}/{file_size}",
                        "Content-Length": str(content_length),
                        "Accept-Ranges": "bytes"
                    }
                )
                await response.prepare(request)
                with open(target_file, "rb") as f:
                    f.seek(start)
                    remaining = content_length
                    while remaining > 0:
                        chunk_size = min(remaining, 256 * 1024)
                        chunk = await asyncio.to_thread(f.read, chunk_size)
                        if not chunk:
                            break
                        await response.write(chunk)
                        remaining -= len(chunk)
                await response.write_eof()
                return response
            except Exception as e:
                logger.debug(f"Range request parsing error for {file_path_str}: {e}")

        # 通常ダウンロード
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes"
            }
        )
        await response.prepare(request)
        with open(target_file, "rb") as f:
            while True:
                chunk = await asyncio.to_thread(f.read, 256 * 1024)
                if not chunk:
                    break
                await response.write(chunk)
        await response.write_eof()
        return response

    async def api_worker_models_download(self, request: web.Request) -> web.StreamResponse:
        """指定された COEIROINK モデルフォルダを tar ストリームで高速配信"""
        model_name = request.match_info.get("model_name", "")
        # 安全性チェック: パストラバーサル防止 (CWE-22)
        clean_model_name = sanitize_filename(model_name)
        if not clean_model_name or not is_safe_identifier(clean_model_name):
            return web.Response(text="Invalid model name", status=400)

        model_dir = Path(os.getenv("COEIROINK_MODEL_DIR", "/app/coeiroink_models")).resolve()
        target_path = validate_safe_path(model_dir, clean_model_name)
        if not target_path or not target_path.exists() or not target_path.is_dir() or not (target_path / "metas.json").exists():
            return web.Response(text="Model not found", status=404)

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "application/x-tar",
                "Content-Disposition": f'attachment; filename="{clean_model_name}.tar"',
            }
        )
        await response.prepare(request)

        # tar -cf - -C /app/coeiroink_models {clean_model_name} を実行し、stdout を直接ストリーミング
        proc = await asyncio.create_subprocess_exec(
            "tar", "-cf", "-", "-C", str(model_dir), clean_model_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )

        try:
            while True:
                chunk = await proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                await response.write(chunk)
            await proc.wait()
            await response.write_eof()
        except asyncio.CancelledError:
            # タスクキャンセル時はプロセス終了してから再raise
            if proc.returncode is None:
                proc.kill()
            raise
        except Exception as e:
            # ConnectionResetError / ClientConnectionResetError / ConnectionError (Connection lost) 等
            # Worker Agent がコンテナ再作成中に接続断が発生するのは正常動作
            # aiohttp の web_protocol.py に伝播させないよう raise せず return する
            if proc.returncode is None:
                proc.kill()
            err_name = type(e).__name__
            if any(k in err_name for k in ("Connection", "Reset", "Transport", "BrokenPipe")):
                logger.warning(
                    f"⚠️ [ModelDownload] Worker との接続が切断されました ({err_name})"
                    " - Worker の再起動中の可能性があります。"
                )
            else:
                logger.error(f"❌ [ModelDownload] 予期しないエラー ({err_name}): {e}", exc_info=True)
        return response

    # --- OSS 動的Bot管理 & クレデンシャル & WebSocket API ---

    async def api_auth_login_password(self, request: web.Request) -> web.Response:
        """管理者パスワードによるWebUIログイン (Discord未連携時対応 & ブルートフォース防御)"""
        # クライアントIP特定
        client_ip = (
            request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote
            or "unknown"
        )
        now = time.time()
        # 直近300秒以内の失敗試行をフィルタ
        attempts = [t for t in self._failed_password_attempts.get(client_ip, []) if now - t < 300]
        self._failed_password_attempts[client_ip] = attempts

        # 5回以上連続失敗で5分間ロックアウト (429 Too Many Requests)
        if len(attempts) >= 5:
            retry_after = int(300 - (now - attempts[0]))
            return web.json_response(
                {"error": f"パスワード試行回数の上限を超えました。{max(1, retry_after)}秒後に再試行してください。"},
                status=429,
                headers={"Retry-After": str(max(1, retry_after))}
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        password = str(data.get("password", "")).strip()
        expected = self.config.admin_password.strip()

        if not expected:
            return web.json_response({
                "error": "管理者パスワードが未設定です。.env の ADMIN_PASSWORD を設定してください。"
            }, status=403)

        import hmac
        if not hmac.compare_digest(password, expected):
            attempts.append(now)
            self._failed_password_attempts[client_ip] = attempts
            # ブルートフォース総当たり対策のバックオフ遅延 (1.0秒)
            await asyncio.sleep(1.0)
            return web.json_response({"error": "パスワードが正しくありません。"}, status=401)

        # ログイン成功時は失敗履歴クリア
        self._failed_password_attempts.pop(client_ip, None)

        user_data = {
            "id": 0,
            "username": "Administrator",
            "global_name": "管理者",
            "avatar": ""
        }
        token = self.auth.create_session_token(user_data, is_admin=True)

        is_https = (
            request.headers.get("X-Forwarded-Proto", "").lower() == "https"
            or "https" in request.headers.get("CF-Visitor", "").lower()
            or request.scheme == "https"
            or self._is_wan_request(request)
        )
        resp = web.json_response({"success": True, "status": "ok", "message": "管理者としてログインしました。"})
        resp.set_cookie(
            "ttsbot_session",
            token,
            max_age=86400 * 7,
            path="/",
            httponly=True,
            samesite="Lax",
            secure=is_https
        )
        return resp

    async def api_system_get_bots(self, request: web.Request) -> web.Response:
        """1〜10号機の全スロット状態を取得 (管理者のみ)"""
        await self._require_admin(request)
        if not self.bot_pool:
            return web.json_response({
                "status": "ok",
                "slots": [],
                "bots": [],
                "total_slots": 10,
                "max_slots": 10,
                "running_count": 0,
                "enabled_count": 0,
                "client_id": self.config.discord_client_id
            })
        status_list = await self.bot_pool.get_full_pool_status()
        running_count = sum(1 for s in status_list if s.get("is_running") or s.get("is_ready"))
        enabled_count = sum(1 for s in status_list if s.get("is_enabled"))
        return web.json_response({
            "status": "ok",
            "slots": status_list,
            "bots": status_list,
            "total_slots": len(status_list),
            "max_slots": 10,
            "running_count": running_count,
            "enabled_count": enabled_count,
            "client_id": self.config.discord_client_id
        })

    async def api_system_save_bot(self, request: web.Request) -> web.Response:
        """Botトークンの追加・更新・即時接続 (管理者のみ)"""
        await self._require_admin(request)
        try:
            try:
                data = await request.json()
            except Exception:
                return web.json_response({"status": "error", "error": "リクエストJSONの形式が不正です"}, status=400)

            bot_index = int(data.get("bot_index", 1))
            token = str(data.get("token", "")).strip()
            label = str(data.get("label", "")).strip()
            is_enabled = bool(data.get("is_enabled", True))

            if not (1 <= bot_index <= 10):
                return web.json_response({"status": "error", "error": "bot_index は 1〜10 の範囲で指定してください。"}, status=400)
            if not token:
                return web.json_response({"status": "error", "error": "Botトークンは必須です。"}, status=400)

            if not self.bot_pool:
                return web.json_response({"status": "error", "error": "BotPool is not initialized."}, status=500)

            success = await self.bot_pool.add_or_update_token(bot_index, token, label=label, is_enabled=is_enabled)
            return web.json_response({
                "status": "ok",
                "success": success,
                "bot_index": bot_index,
                "message": f"Bot #{bot_index} のトークンを保存・接続開始しました。"
            })
        except Exception as e:
            logger.exception("Failed to save bot token")
            return web.json_response({
                "status": "error",
                "error": f"Botトークン保存中にエラーが発生しました: {e}"
            }, status=500)

    async def api_system_delete_bot(self, request: web.Request) -> web.Response:
        """Botトークンの削除および停止 (管理者のみ)"""
        await self._require_admin(request)
        idx_str = request.match_info.get("idx")
        try:
            bot_index = int(idx_str)
        except (ValueError, TypeError):
            return web.json_response({"status": "error", "error": "無効なBotインデックスです"}, status=400)

        if not self.bot_pool:
            return web.json_response({"status": "error", "error": "BotPool is not initialized."}, status=500)

        try:
            await self.bot_pool.delete_token(bot_index)
            return web.json_response({
                "status": "ok",
                "success": True,
                "bot_index": bot_index,
                "message": f"Bot #{bot_index} を切断・削除しました。"
            })
        except Exception as e:
            logger.exception(f"Failed to delete bot #{bot_index}")
            return web.json_response({
                "status": "error",
                "error": f"Bot削除中にエラーが発生しました: {e}"
            }, status=500)

    async def api_system_toggle_bot(self, request: web.Request) -> web.Response:
        """Botの有効/無効切替 (管理者のみ)"""
        await self._require_admin(request)
        idx_str = request.match_info.get("idx")
        try:
            bot_index = int(idx_str)
        except (ValueError, TypeError):
            return web.json_response({"status": "error", "error": "無効なBotインデックスです"}, status=400)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"status": "error", "error": "リクエストJSONの形式が不正です"}, status=400)

        is_enabled = bool(data.get("is_enabled", True))
        if not self.bot_pool:
            return web.json_response({"status": "error", "error": "BotPool is not initialized."}, status=500)

        try:
            success = await self.bot_pool.toggle_token(bot_index, is_enabled)
            return web.json_response({
                "status": "ok",
                "success": success,
                "bot_index": bot_index,
                "is_enabled": is_enabled
            })
        except Exception as e:
            logger.exception(f"Failed to toggle bot #{bot_index}")
            return web.json_response({
                "status": "error",
                "error": f"Bot状態変更中にエラーが発生しました: {e}"
            }, status=500)

    async def api_system_get_credentials(self, request: web.Request) -> web.Response:
        """APIキー・設定情報の取得 (伏字表示, 管理者のみ)"""
        await self._require_admin(request)

        def _mask(val: str, keep_tail: int = 4) -> str:
            if not val:
                return ""
            if len(val) <= keep_tail:
                return "••••••••"
            return f"{'•' * (len(val) - keep_tail)}{val[-keep_tail:]}"

        has_gcp_json = bool(
            self.config.google_credentials_json or
            (self.config.google_application_credentials and os.path.exists(self.config.google_application_credentials)) or
            os.path.exists("gcp-key.json") or
            os.path.exists("/app/gcp-key.json")
        )
        is_google_configured = bool(
            has_gcp_json or
            self.config.google_api_key or
            os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        )

        tunnel_info = await self._get_docker_tunnel_status()
        cf_token = self.config.cloudflare_tunnel_token or (tunnel_info.get("token_in_container") if tunnel_info.get("has_token") else "")

        # システム共通エラー通知先DiscordユーザーID
        system_contact_id = await self.db.get_credential("error_contact_user_id")
        if not system_contact_id:
            from src.utils.alert_service import alert_service
            system_contact_id = str(alert_service.INITIAL_DEFAULT_CONTACT_ID)

        # HA共有認証シークレット (Master-Slave間)
        ha_secret = self.config.ha_secret
        if not ha_secret:
            ha_secret = await self.db.get_credential("ha_secret")
        if not ha_secret:
            import secrets
            ha_secret = secrets.token_urlsafe(24)
            await self.db.set_credential("ha_secret", ha_secret, is_secret=True)
            self.config.ha_secret = ha_secret
            logger.info("🔐 Generated and saved default TTSBOT_HA_SECRET for cluster HA.")
        else:
            self.config.ha_secret = ha_secret

        return web.json_response({
            "status": "ok",
            "azure_speech_key": _mask(self.config.azure_speech_key),
            "azure_speech_key_configured": bool(self.config.azure_speech_key),
            "azure_speech_configured": bool(self.config.azure_speech_key),
            "azure_speech_region": self.config.azure_speech_region or "japaneast",
            "google_configured": is_google_configured,
            "google_tts_configured": is_google_configured,
            "has_google_json": has_gcp_json,
            "google_api_key": _mask(self.config.google_api_key),
            "worker_shared_secret": self.config.worker_shared_secret,
            "web_base_url": self.config.web_dashboard_url,
            "admin_password_configured": bool(self.config.admin_password),
            # Cloudflare Tunnel 状態
            "cloudflare_tunnel_configured": bool(cf_token),
            "cloudflare_tunnel_token": _mask(cf_token),
            "cloudflare_tunnel_status": tunnel_info.get("status", "not_created"),
            "cloudflare_tunnel_running": bool(tunnel_info.get("running", False)),
            "cloudflare_tunnel_container_id": tunnel_info.get("container_id", ""),
            # システム共通エラー通知先
            "error_contact_user_id": str(system_contact_id),
            # HA共有認証シークレット
            "ha_secret": self.config.ha_secret
        })

    async def api_system_save_credentials(self, request: web.Request) -> web.Response:
        """APIキー・設定の保存＆ホットリロード (管理者のみ)"""
        await self._require_admin(request)
        try:
            try:
                data = await request.json()
            except Exception:
                return web.json_response({"status": "error", "error": "リクエストJSONの形式が不正です"}, status=400)

            # 1. Azure Speech Key
            if "azure_speech_key" in data and not str(data["azure_speech_key"]).startswith("•"):
                k = str(data["azure_speech_key"]).strip()
                await self.db.set_credential("azure_speech_key", k, is_secret=True)
                self.config.azure_speech_key = k

            # 2. Azure Speech Region
            if "azure_speech_region" in data:
                reg = str(data["azure_speech_region"]).strip() or "japaneast"
                await self.db.set_credential("azure_speech_region", reg)
                self.config.azure_speech_region = reg

            # 3. Google Service Account JSON (google_credentials_json or google_service_account_json)
            raw_g_json = None
            if "google_credentials_json" in data:
                raw_g_json = data["google_credentials_json"]
            elif "google_service_account_json" in data:
                raw_g_json = data["google_service_account_json"]

            if raw_g_json is not None and not str(raw_g_json).startswith("•"):
                g_json = str(raw_g_json).strip()
                if g_json:
                    # JSON構文および必須フィールドのバリデーション
                    try:
                        parsed_sa = json.loads(g_json)
                    except Exception as e:
                        return web.json_response({
                            "status": "error",
                            "error": f"サービスアカウントJSONの形式が不正です (JSON構文エラー: {e})"
                        }, status=400)

                    if not isinstance(parsed_sa, dict):
                        return web.json_response({
                            "status": "error",
                            "error": "サービスアカウントJSONはオブジェクト形式である必要があります"
                        }, status=400)

                    required_fields = ["project_id", "private_key", "client_email"]
                    missing = [f for f in required_fields if not parsed_sa.get(f)]
                    if missing:
                        return web.json_response({
                            "status": "error",
                            "error": f"サービスアカウントJSONに必要な項目が不足しています: {', '.join(missing)}"
                        }, status=400)

                    # 正規化されたJSON文字列として保存
                    normalized_json = json.dumps(parsed_sa, indent=2, ensure_ascii=False)
                    await self.db.set_credential("google_credentials_json", normalized_json, is_secret=True)
                    self.config.google_credentials_json = normalized_json

                    # ホスト/コンテナ内の gcp-key.json ファイルにも同期保存
                    for path in ["gcp-key.json", "/app/gcp-key.json"]:
                        try:
                            with open(path, "w", encoding="utf-8") as f:
                                f.write(normalized_json)
                            logger.info(f"💾 Synchronized GCP service account JSON to {path}")
                            break
                        except Exception as e:
                            logger.debug(f"Could not write to {path}: {e}")

            # 4. Google API Key
            if "google_api_key" in data and not str(data["google_api_key"]).startswith("•"):
                g_key = str(data["google_api_key"]).strip()
                if g_key:
                    await self.db.set_credential("google_api_key", g_key, is_secret=True)
                    self.config.google_api_key = g_key

            # 5. Worker Shared Secret
            if "worker_shared_secret" in data:
                w_sec = str(data["worker_shared_secret"]).strip()
                if w_sec:
                    await self.db.set_credential("worker_shared_secret", w_sec, is_secret=True)
                    self.config.worker_shared_secret = w_sec

            # 6. Web Base URL
            if "web_base_url" in data:
                b_url = str(data["web_base_url"]).strip().rstrip("/")
                if b_url:
                    await self.db.set_credential("web_base_url", b_url)
                    self.config.web_dashboard_url = b_url
                    self.config.discord_redirect_uri = f"{b_url}/callback"

            # 7. Admin Password
            if "admin_password" in data:
                new_pwd = str(data["admin_password"]).strip()
                if new_pwd:
                    await self.db.set_credential("admin_password", new_pwd, is_secret=True)
                    self.config.admin_password = new_pwd

            # 8. Cloudflare Tunnel Token & 自動起動連動
            tunnel_message = ""
            if "cloudflare_tunnel_token" in data:
                raw_cf_token = str(data["cloudflare_tunnel_token"]).strip()
                if not raw_cf_token.startswith("•"):
                    old_token = (self.config.cloudflare_tunnel_token or "").strip()
                    if raw_cf_token:
                        # トークン保存
                        await self.db.set_credential("cloudflare_tunnel_token", raw_cf_token, is_secret=True)
                        self.config.cloudflare_tunnel_token = raw_cf_token
                        self._sync_cloudflare_token_to_env(raw_cf_token)

                        # 「Tokenが無いところから投入されたら起動する」
                        # またはTokenが変更された場合、あるいは現在停止中なら再作成・起動
                        tunnel_stat = await self._get_docker_tunnel_status()
                        if not old_token or old_token != raw_cf_token or not tunnel_stat.get("running"):
                            succ, t_msg = await self._start_or_update_docker_tunnel(raw_cf_token)
                            tunnel_message = f" ({t_msg})"
                    else:
                        # 空文字が明示的に送信された場合はクリア＆停止
                        if old_token:
                            await self.db.set_credential("cloudflare_tunnel_token", "", is_secret=False)
                            self.config.cloudflare_tunnel_token = ""
                            self._sync_cloudflare_token_to_env("")
                            succ, t_msg = await self._stop_docker_tunnel()
                            tunnel_message = f" ({t_msg})"

            # 9. システム共通エラー連絡先 (Discord ユーザーID)
            if "error_contact_user_id" in data:
                raw_ec_id = str(data["error_contact_user_id"]).strip()
                if raw_ec_id.isdigit():
                    await self.db.set_credential("error_contact_user_id", raw_ec_id)
                elif not raw_ec_id:
                    from src.utils.alert_service import alert_service
                    await self.db.set_credential("error_contact_user_id", str(alert_service.INITIAL_DEFAULT_CONTACT_ID))

            # ホットリロード
            if self.tts_manager:
                self.tts_manager.reload_cloud_credentials(
                    azure_key=self.config.azure_speech_key,
                    azure_region=self.config.azure_speech_region,
                    google_json=self.config.google_credentials_json,
                    google_api_key=self.config.google_api_key
                )

            return web.json_response({
                "status": "ok",
                "success": True,
                "message": f"設定を保存し、ホットリロードしました。{tunnel_message}"
            })
        except Exception as e:
            logger.exception("api_system_save_credentials failed")
            return web.json_response({"status": "error", "error": f"保存中にエラーが発生しました: {e}"}, status=500)

    async def api_system_alert_test(self, request: web.Request) -> web.Response:
        """エラー連絡先へのテストDM送信"""
        try:
            try:
                data = await request.json()
            except Exception:
                data = {}

            guild_id_raw = data.get("guild_id")
            guild_id = int(guild_id_raw) if guild_id_raw and str(guild_id_raw).isdigit() else None

            # 権限チェック: ギルドIDがある場合はギルドの管理者/設定ロール、ない場合はシステム管理者
            if guild_id:
                try:
                    guild, perms = await self._get_guild_and_perms(request, guild_id)
                    if not (perms["is_admin"] or perms["has_role"]):
                        return web.json_response({"status": "error", "error": "権限がありません (管理者または設定ロールが必要)"}, status=403)
                except Exception:
                    await self._require_admin(request)
            else:
                await self._require_admin(request)

            target_user_id_raw = data.get("target_user_id")
            explicit_target_id = int(target_user_id_raw) if target_user_id_raw and str(target_user_id_raw).isdigit() else None

            from src.utils.alert_service import alert_service, SEVERITY_INFO, CATEGORY_BOT

            resolved_id = await alert_service.resolve_target_user_id(guild_id=guild_id, explicit_target_id=explicit_target_id)
            if not resolved_id or resolved_id <= 0:
                return web.json_response({
                    "status": "error",
                    "error": "通知先のDiscordユーザーIDが未指定または無効です"
                }, status=400)

            if not alert_service.bot or not alert_service.bot.is_ready():
                return web.json_response({
                    "status": "error",
                    "error": "Discord Bot が準備中または未接続のため、DMを送信できません。しばらく待ってから再試行してください。"
                }, status=503)

            # ユーザー確認
            try:
                user = alert_service.bot.get_user(resolved_id)
                if not user:
                    user = await alert_service.bot.fetch_user(resolved_id)
            except Exception as e:
                return web.json_response({
                    "status": "error",
                    "error": f"Discord ユーザー (ID: {resolved_id}) が見つかりませんでした: {e}"
                }, status=400)

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            test_title = "🔔 エラー連絡先 DM 通知テスト"
            test_msg = (
                f"TTSBot のエラー・障害通知先テストメッセージです。\n"
                f"このDMを受信できている場合、通知先設定 (ID: `{resolved_id}`) は正常に機能しています。\n"
                f"送信日時: {now_str}"
            )
            test_details = f"送信元: {'サーバー設定 (Guild: ' + str(guild_id) + ')' if guild_id else 'システム共通設定'}"

            success = await alert_service.send_alert(
                title=test_title,
                message=test_msg,
                category=CATEGORY_BOT,
                severity=SEVERITY_INFO,
                details=test_details,
                cooldown_seconds=0,
                guild_id=guild_id,
                target_user_id=resolved_id
            )

            if success:
                return web.json_response({
                    "status": "ok",
                    "success": True,
                    "target_user_id": str(resolved_id),
                    "target_user_name": str(user),
                    "message": f"Discord ユーザー「{user}」(ID: {resolved_id}) 宛てにテストDMを送信しました！"
                })
            else:
                return web.json_response({
                    "status": "error",
                    "error": f"テストDM送信に失敗しました。ユーザー「{user}」がBotからのDM受信を拒否しているか、共通サーバーに存在しない可能性があります。"
                }, status=500)
        except Exception as e:
            logger.exception("api_system_alert_test failed")
            return web.json_response({"status": "error", "error": f"テスト送信中にエラーが発生しました: {e}"}, status=500)

    # --------------------------------------------------------------------------
    # Docker Engine API (/var/run/docker.sock) 連携 & Cloudflare Tunnel 制御
    # --------------------------------------------------------------------------
    async def _docker_request(self, method: str, path: str, json_data: Any = None) -> Tuple[int, Any]:
        """Docker Engine API (/var/run/docker.sock) への非同期リクエスト"""
        socket_path = "/var/run/docker.sock"
        if not os.path.exists(socket_path):
            return 503, {"error": "docker.sock not found"}
        try:
            connector = aiohttp.UnixConnector(path=socket_path)
            async with aiohttp.ClientSession(connector=connector) as session:
                url = f"http://localhost{path}"
                async with session.request(method, url, json=json_data) as resp:
                    try:
                        data = await resp.json()
                    except Exception:
                        data = await resp.text()
                    return resp.status, data
        except Exception as e:
            logger.warning(f"Docker API error ({method} {path}): {e}")
            return 500, {"error": str(e)}

    async def _get_docker_tunnel_status(self) -> Dict[str, Any]:
        """Cloudflare Tunnel (ttsbot-tunnel-1) コンテナの稼働ステータスを取得"""
        status_code, data = await self._docker_request("GET", "/containers/ttsbot-tunnel-1/json")
        if status_code == 200 and isinstance(data, dict):
            state = data.get("State", {})
            config = data.get("Config", {})
            env_list = config.get("Env", [])
            token_in_container = ""
            for item in env_list:
                if item.startswith("TUNNEL_TOKEN="):
                    token_in_container = item[len("TUNNEL_TOKEN="):]
                    break
            return {
                "exists": True,
                "running": bool(state.get("Running", False)),
                "status": state.get("Status", "unknown"),
                "started_at": state.get("StartedAt"),
                "container_id": data.get("Id", "")[:12],
                "has_token": bool(token_in_container.strip()),
                "token_in_container": token_in_container.strip(),
                "image": config.get("Image")
            }
        elif status_code == 404:
            return {
                "exists": False,
                "running": False,
                "status": "not_created",
                "has_token": False
            }
        else:
            return {
                "exists": False,
                "running": False,
                "status": "error",
                "error": str(data),
                "has_token": False
            }

    async def _start_or_update_docker_tunnel(self, token: str) -> Tuple[bool, str]:
        """Cloudflare Tunnel (ttsbot-tunnel-1) を指定トークンで起動・再作成"""
        if not token or not token.strip():
            return False, "有効な Cloudflare Tunnel Token を指定してください"
        token = token.strip()

        # 既存コンテナの存在確認
        info = await self._get_docker_tunnel_status()
        if info.get("exists"):
            # すでに同一トークンで起動中であれば何もしない
            if info.get("running") and info.get("token_in_container") == token:
                logger.info("Cloudflare Tunnel is already running with the same token.")
                return True, "Cloudflare Tunnel は既に同一トークンで稼働中です"

            logger.info("Stopping and removing existing ttsbot-tunnel-1 container...")
            await self._docker_request("POST", "/containers/ttsbot-tunnel-1/stop?t=5")
            await self._docker_request("DELETE", "/containers/ttsbot-tunnel-1?force=true")

        # 新規コンテナ作成 (docker-compose.yml の定義と完全互換)
        create_payload = {
            "Image": "cloudflare/cloudflared:latest",
            "Cmd": ["tunnel", "--no-autoupdate", "run"],
            "Env": [
                f"TUNNEL_TOKEN={token}",
                "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
            ],
            "HostConfig": {
                "RestartPolicy": {"Name": "unless-stopped"},
                "NetworkMode": "ttsbot_default",
                "NanoCpus": 500000000,
                "Memory": 268435456
            },
            "Labels": {
                "com.docker.compose.project": "ttsbot",
                "com.docker.compose.service": "tunnel"
            }
        }
        status_code, resp_data = await self._docker_request("POST", "/containers/create?name=ttsbot-tunnel-1", create_payload)
        if status_code not in (200, 201):
            err_msg = f"Tunnelコンテナ作成に失敗しました (Status {status_code}): {resp_data}"
            logger.error(err_msg)
            return False, err_msg

        container_id = resp_data.get("Id", "ttsbot-tunnel-1")
        start_code, start_resp = await self._docker_request("POST", f"/containers/{container_id}/start")
        if start_code not in (200, 204, 304):
            err_msg = f"Tunnelコンテナ起動に失敗しました (Status {start_code}): {start_resp}"
            logger.error(err_msg)
            return False, err_msg

        logger.info(f"🚀 Cloudflare Tunnel container started successfully (ID: {container_id[:12]})")
        return True, "Cloudflare Tunnel を起動しました"

    async def _stop_docker_tunnel(self) -> Tuple[bool, str]:
        """Cloudflare Tunnel コンテナを停止"""
        info = await self._get_docker_tunnel_status()
        if not info.get("exists"):
            return True, "Cloudflare Tunnel は起動していません"

        await self._docker_request("POST", "/containers/ttsbot-tunnel-1/stop?t=5")
        return True, "Cloudflare Tunnel を停止しました"

    def _sync_cloudflare_token_to_env(self, token: str) -> None:
        """ホスト/コンテナ内の .env ファイルの CLOUDFLARE_TUNNEL_TOKEN を同期更新"""
        for env_file in [Path(".env"), Path("/app/.env")]:
            if env_file.exists():
                try:
                    content = env_file.read_text(encoding="utf-8")
                    if "CLOUDFLARE_TUNNEL_TOKEN=" in content:
                        new_content = re.sub(r'CLOUDFLARE_TUNNEL_TOKEN=.*', f'CLOUDFLARE_TUNNEL_TOKEN={token}', content)
                    else:
                        new_content = content.rstrip() + f"\nCLOUDFLARE_TUNNEL_TOKEN={token}\n"
                    env_file.write_text(new_content, encoding="utf-8")
                    logger.info(f"💾 Synced CLOUDFLARE_TUNNEL_TOKEN to {env_file}")
                    break
                except Exception as e:
                    logger.warning(f"Could not update {env_file}: {e}")

    async def api_system_tunnel_restart(self, request: web.Request) -> web.Response:
        """Cloudflare Tunnel 手動再起動 (管理者のみ)"""
        await self._require_admin(request)
        token = (self.config.cloudflare_tunnel_token or "").strip()
        if not token:
            t_stat = await self._get_docker_tunnel_status()
            token = (t_stat.get("token_in_container") or "").strip()
        if not token:
            return web.json_response({
                "status": "error",
                "error": "Cloudflare Tunnel Token が設定されていません"
            }, status=400)

        success, msg = await self._start_or_update_docker_tunnel(token)
        return web.json_response({
            "status": "ok" if success else "error",
            "success": success,
            "message": msg
        })

    async def api_system_tunnel_stop(self, request: web.Request) -> web.Response:
        """Cloudflare Tunnel 手動停止 (管理者のみ)"""
        await self._require_admin(request)
        success, msg = await self._stop_docker_tunnel()
        return web.json_response({
            "status": "ok" if success else "error",
            "success": success,
            "message": msg
        })

    async def api_system_test_credentials(self, request: web.Request) -> web.Response:
        """指定TTSプロバイダの即時疎通・音声合成テスト (管理者のみ)"""
        await self._require_admin(request)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"status": "error", "error": "Invalid JSON"}, status=400)

        provider = str(data.get("provider", "azure")).strip()
        voice_name = str(data.get("voice_name", "")).strip()

        if not self.tts_manager:
            return web.json_response({
                "status": "error",
                "success": False,
                "error": "TTSManager is not initialized."
            }, status=500)

        success, message, elapsed_ms, wav_bytes = await self.tts_manager.test_synthesis(provider, voice_name)
        audio_b64 = None
        if success and wav_bytes:
            audio_b64 = base64.b64encode(wav_bytes).decode("ascii")

        return web.json_response({
            "status": "ok" if success else "error",
            "success": success,
            "provider": provider,
            "message": message,
            "latency_ms": elapsed_ms,
            "format": "wav",
            "audio_base64": audio_b64,
            "error": message if not success else None
        })

    async def api_system_get_tier_routing(self, request: web.Request) -> web.Response:
        """エンジン毎の動的 Tier ルーティング設定を取得 (管理者のみ)"""
        await self._require_admin(request)
        from src.tts.node_pool import (
            get_all_tier_routing_orders,
            DEFAULT_TIER_ROUTING_ORDER,
            SUPPORTED_ROUTING_ENGINES,
            TIER_METADATA
        )
        return web.json_response({
            "status": "ok",
            "engines": get_all_tier_routing_orders(),
            "default_order": list(DEFAULT_TIER_ROUTING_ORDER),
            "supported_engines": SUPPORTED_ROUTING_ENGINES,
            "metadata": TIER_METADATA
        })

    async def api_system_save_tier_routing(self, request: web.Request) -> web.Response:
        """エンジン毎の動的 Tier ルーティング設定を保存 (管理者のみ)"""
        await self._require_admin(request)
        from src.tts.node_pool import save_tier_routing_to_disk, get_all_tier_routing_orders
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"status": "error", "message": "Invalid JSON body"}, status=400)

        engine_orders: Dict[str, List[str]] = {}
        valid_tier_keys = {"gpu_ram", "cpu_ram", "gpu_ssd", "cpu_ssd"}

        if "engines" in body and isinstance(body["engines"], dict):
            for eng, ord_list in body["engines"].items():
                if isinstance(ord_list, list) and set(ord_list) == valid_tier_keys and len(ord_list) == 4:
                    engine_orders[str(eng).lower()] = ord_list
        elif "engine" in body and "tier_order" in body:
            eng = str(body["engine"]).strip().lower()
            ord_list = body["tier_order"]
            if isinstance(ord_list, list) and set(ord_list) == valid_tier_keys and len(ord_list) == 4:
                engine_orders[eng] = ord_list
            else:
                return web.json_response({
                    "status": "error",
                    "message": "tier_order must contain exactly 4 unique keys: gpu_ram, cpu_ram, gpu_ssd, cpu_ssd"
                }, status=400)
        else:
            return web.json_response({"status": "error", "message": "Missing 'engine'/'tier_order' or 'engines' in request"}, status=400)

        if not engine_orders:
            return web.json_response({"status": "error", "message": "No valid engine routing configuration provided"}, status=400)

        success = save_tier_routing_to_disk(engine_orders)
        if success:
            return web.json_response({
                "status": "ok",
                "message": "Tier routing order successfully saved",
                "engines": get_all_tier_routing_orders()
            })
        else:
            return web.json_response({"status": "error", "message": "Failed to persist tier routing configuration"}, status=500)

    async def api_worker_ws(self, request: web.Request) -> web.WebSocketResponse:
        """
        リモートボランティアWorkerノード用 WebSocket ゲートウェイ
        Worker (外部PC/GPUノード等) が外向きにMasterへ常時接続し、Masterからの音声合成要求を双方向中継する
        """
        if not self._verify_worker_auth(request):
            raise web.HTTPUnauthorized(text="Unauthorized worker token")

        ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=32 * 1024 * 1024)
        await ws.prepare(request)

        ws_node: Optional[Any] = None
        worker_node_id: Optional[str] = None
        logger.info(f"🔌 [WorkerWS] New WebSocket connection from {request.remote}")

        try:
            from src.tts.ws_node_adapter import WSWorkerNode
            from src.tts.protocols.worker_protocol import (
                unpack_binary_chunk,
                HeartbeatMessage
            )

            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    if ws_node is not None:
                        try:
                            req_id, chunk_index, is_final, payload = unpack_binary_chunk(msg.data)
                            ws_node.on_binary_chunk(req_id, chunk_index, is_final, payload)
                        except Exception as e:
                            logger.warning(f"⚠️ [WorkerWS] Error handling binary chunk from {worker_node_id}: {e}")

                elif msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        continue

                    msg_type = data.get("type")
                    if msg_type == "register":
                        worker_node_id = data.get("node_id", f"ws_worker_{int(time.time())}")
                        node_name = data.get("node_name", worker_node_id)
                        supported_engines = data.get("supported_engines", ["voicevox"])
                        supported_models = data.get("supported_models", {})
                        capacity = int(data.get("capacity", 2))
                        specs = data.get("specs", {})
                        priority = int(data.get("priority", 50))
                        exclusive_guild_id = data.get("exclusive_guild_id")
                        is_cluster_worker = bool(data.get("is_cluster_worker") or str(worker_node_id).startswith("worker_"))

                        # 既に常設 HTTP クラスタノードとしてプール内に登録されているかチェック
                        has_active_http_node = False
                        for p in (self.tts_manager.voicevox_pool, self.tts_manager.sharevox_pool, self.tts_manager.coeiroink_pool):
                            if hasattr(p, "nodes"):
                                for n in p.nodes:
                                    nid = getattr(n, "id", "")
                                    if (nid == worker_node_id or nid.startswith(f"{worker_node_id}_")) and not getattr(n, "is_ws", False):
                                        has_active_http_node = True
                                        break
                            if has_active_http_node:
                                break

                        # WS 接続の保持
                        self.worker_ws_connections[worker_node_id] = ws

                        if has_active_http_node:
                            # 既に HTTP クラスタノードとして健全稼働中のため、重複するボランティアノードとしてのプール追加を抑止
                            logger.info(f"ℹ️ [WorkerWS] Node '{worker_node_id}' already has active HTTP cluster engines. Skipping duplicate volunteer pool registration.")
                        else:
                            # WSWorkerNode を生成して TTSManager のプール群へ登録
                            ws_node = WSWorkerNode(
                                node_id=worker_node_id,
                                node_name=node_name,
                                ws=ws,
                                supported_engines=supported_engines,
                                capacity=capacity,
                                specs=specs,
                                priority=priority,
                                exclusive_guild_id=exclusive_guild_id,
                                supported_models=supported_models
                            )
                            # クラスタノードとして接続された場合は is_volunteer を False に設定
                            if is_cluster_worker:
                                ws_node.is_volunteer = False
                            self.tts_manager.register_ws_worker_node(ws_node)

                        await ws.send_json({
                            "type": "register_ack",
                            "status": "ok",
                            "node_id": worker_node_id,
                            "exclusive_guild_id": exclusive_guild_id,
                            "heartbeat_interval_sec": 10.0
                        })
                        scope_desc = f" (Dedicated to Guild {exclusive_guild_id})" if exclusive_guild_id else " (Global Shared)"
                        models_desc = f" - Models: {list(supported_models.keys())}" if supported_models else ""
                        node_type_desc = "Cluster Worker Tunnel" if is_cluster_worker else "Volunteer Worker"
                        logger.info(f"✅ [WorkerWS] {node_type_desc} connected: {worker_node_id} ({node_name}){scope_desc} - Engines: {supported_engines}{models_desc}")

                    elif msg_type == "heartbeat":
                        if ws_node is not None:
                            hb = HeartbeatMessage(
                                node_id=worker_node_id or "",
                                active_tasks=int(data.get("active_tasks", 0)),
                                cpu_percent=float(data.get("cpu_percent", 0.0)),
                                memory_percent=float(data.get("memory_percent", 0.0)),
                                engine_status=data.get("engine_status", {}),
                                uptime_sec=float(data.get("uptime_sec", 0.0)),
                                supported_models=data.get("supported_models", {})
                            )
                            ws_node.update_from_heartbeat(hb)
                        await ws.send_json({"type": "heartbeat_ack", "server_time": time.time()})

                    elif msg_type == "synthesize_error":
                        if ws_node is not None:
                            req_id = data.get("req_id", "")
                            err_msg = data.get("error_message", "Unknown worker error")
                            ws_node.on_error(req_id, err_msg)

                    elif msg_type == "synthesize_response":
                        # 後方互換性: Base64フォールバック
                        req_id = data.get("req_id")
                        if ws_node is not None and req_id:
                            if data.get("error"):
                                ws_node.on_error(req_id, data["error"])
                            else:
                                import base64
                                audio_bytes = base64.b64decode(data.get("audio_b64", ""))
                                ws_node.on_binary_chunk(req_id, 0, True, audio_bytes)

                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                    break
        finally:
            if ws_node is not None:
                await ws_node.close()
                self.tts_manager.unregister_ws_worker_node(ws_node.id)
            if worker_node_id:
                self.worker_ws_connections.pop(worker_node_id, None)
                logger.info(f"🔌 [WorkerWS] Volunteer Worker disconnected: {worker_node_id}")

        return ws

    # --- ライフサイクル ---

    async def start(self):
        """Webサーバーを非同期起動"""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.config.web_host, self.config.web_port)
        await self.site.start()
        logger.info(f"WebUI server started on http://{self.config.web_host}:{self.config.web_port}")

    async def stop(self):
        """Webサーバーを安全に停止"""
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        logger.info("WebUI server stopped.")
