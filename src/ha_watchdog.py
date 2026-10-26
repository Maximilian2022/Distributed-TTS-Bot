#!/usr/bin/env python3
# ==============================================================================
# TTSBot HA Watchdog & DB Synchronizer (Slave/Standby Daemon)
# ==============================================================================
# Master-Slave Active-Standby 高可用性オーケストレータ
# - 平常時: Master の死活監視 & /api/ha/db_snapshot からの暗号化DB自動プル同期
# - 障害時: Master 無応答検知で Slave を自律 Active 昇格 (Failover)
# - 復帰時: Master 復旧検知で Slave を自律 Standby 降格 (Preempt Demote)
# ==============================================================================

import os
import sys
import time
import gzip
import json
import logging
import asyncio
import hashlib
import tempfile
import io
import tarfile
import urllib.request
import urllib.error
import http.client
import py_compile
import shutil
import socket
import uuid
import random
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [HA-Watchdog] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout
)
logger = logging.getLogger("HAWatchdog")


class DockerSocketClient:
    """Unix Socket (/var/run/docker.sock) 経由で Docker Engine API を直接制御する軽量クライアント"""

    def __init__(self, socket_path: str = "/var/run/docker.sock"):
        self.socket_path = socket_path

    def is_available(self) -> bool:
        return os.path.exists(self.socket_path)

    def _send_request(self, method: str, path: str, body: Optional[bytes] = None) -> Tuple[int, bytes]:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(10.0)
            sock.connect(self.socket_path)
            req = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
            if body:
                req += f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
                sock.sendall(req.encode("utf-8") + body)
            else:
                req += "\r\n"
                sock.sendall(req.encode("utf-8"))

            res_data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                res_data += chunk

            header_part, _, body_part = res_data.partition(b"\r\n\r\n")
            lines = header_part.split(b"\r\n")
            status_code = 500
            if lines:
                parts = lines[0].split(b" ")
                if len(parts) >= 2 and parts[1].isdigit():
                    status_code = int(parts[1])
            return status_code, body_part
        finally:
            sock.close()

    def find_containers_by_prefix(self, prefix: str) -> list:
        status, body = self._send_request("GET", "/containers/json?all=1")
        if status != 200:
            return []
        try:
            containers = json.loads(body.decode("utf-8"))
            results = []
            for c in containers:
                names = [n.lstrip("/") for n in c.get("Names", [])]
                if any(n.startswith(prefix) for n in names):
                    results.append(c)
            return results
        except Exception:
            return []

    def find_app_containers(self) -> list:
        """フェイルオーバー昇格対象の本体 (app) コンテナを確実に特定 (watchdog/tunnel等の補助コンテナは除外)"""
        status, body = self._send_request("GET", "/containers/json?all=1")
        if status != 200:
            return []
        try:
            containers = json.loads(body.decode("utf-8"))
            results = []
            for c in containers:
                names = [n.lstrip("/") for n in c.get("Names", [])]
                for n in names:
                    # ttsbot-app, app, ttsbot-slave-app 等にマッチし、自身(watchdog)やtunnel等は除外
                    if ("app" in n.lower() or "ttsbot" in n.lower()) and not any(ex in n.lower() for ex in ("watchdog", "tunnel", "nginx", "proxy", "agent", "worker")):
                        results.append(c)
                        break
            return results
        except Exception:
            return []

    def find_tunnel_containers(self) -> list:
        """フェイルオーバー昇格・降格対象の Cloudflare Tunnel コンテナを特定 (watchdog等は除外)"""
        status, body = self._send_request("GET", "/containers/json?all=1")
        if status != 200:
            return []
        try:
            containers = json.loads(body.decode("utf-8"))
            results = []
            for c in containers:
                names = [n.lstrip("/") for n in c.get("Names", [])]
                for n in names:
                    # ttsbot-slave-tunnel, ttsbot-tunnel, tunnel 等にマッチし、自身(watchdog)や他アプリは除外
                    if "tunnel" in n.lower() and not any(ex in n.lower() for ex in ("watchdog", "nginx", "proxy", "agent", "worker")):
                        results.append(c)
                        break
            return results
        except Exception:
            return []

    def start_container(self, container_id: str) -> bool:
        status, _ = self._send_request("POST", f"/containers/{container_id}/start")
        return status in (204, 304)

    def stop_container(self, container_id: str, timeout_sec: int = 5) -> bool:
        status, _ = self._send_request("POST", f"/containers/{container_id}/stop?t={timeout_sec}")
        return status in (204, 304)


import platform

class HAWatchdog:
    """Master-Slave HA 監視・同期・調停マネージャー (複数台カスケード Priority 制御対応)"""

    def __init__(self):
        self.master_url = os.getenv("TTSBOT_MASTER_URL", "").strip().rstrip("/")
        if not self.master_url:
            self.master_url = os.getenv("TTSBOT_PRIMARY_HEALTH_URL", "").strip().rstrip("/")
            if self.master_url.endswith("/health"):
                self.master_url = self.master_url[:-7]

        self.ha_secret = os.getenv("TTSBOT_HA_SECRET", os.getenv("HA_SECRET", "")).strip()
        self.check_interval = float(os.getenv("TTSBOT_CHECK_INTERVAL", "5.0"))
        self.base_fail_threshold = int(os.getenv("TTSBOT_FAIL_THRESHOLD", "6"))  # 基本: 6回 (約30秒)
        self.priority = int(os.getenv("TTSBOT_HA_PRIORITY", "1"))  # 優先度 (1=最速昇格、2=待機昇格)
        # 待機時間を十分確保 (Priority 1: 30秒, Priority 2: 3分, Priority 3: 5分)
        # Cloudflare Tunnel の切り替えや先行ノードの起動完了を待つため
        self.fail_threshold = self._calc_fail_threshold(self.priority)
        self.sync_interval = float(os.getenv("TTSBOT_SYNC_INTERVAL", "60.0"))  # 1分周期でDB同期
        self.db_path = Path(os.getenv("TTSBOT_LOCAL_DB_PATH", "/app/data/bot.db"))
        self.docker = DockerSocketClient(os.getenv("TTSBOT_DOCKER_SOCKET", "/var/run/docker.sock"))

        # システム情報・ホスト実体検出
        try:
            from src.utils.system_info import get_host_system_info
            self.host_info = get_host_system_info()
        except Exception:
            self.host_info = {}

        self.hostname = self.host_info.get("hostname") or socket.gethostname()
        self.container_hostname = self.host_info.get("container_hostname") or socket.gethostname()
        self.node_id = self._init_node_id()

        # クラスタトポロジー (他ノード情報) の保持
        self.cluster_nodes_file = self.db_path.parent / "ha_cluster_nodes.json"
        self.cluster_nodes: List[Dict[str, Any]] = self._load_cluster_nodes()

        # システムスペックの自動検出 (Master での Priority 自動提案・表示用)
        self.cpu_cores = os.cpu_count() or 1
        self.memory_gb = self._detect_memory_gb()
        self.os_name = self.host_info.get("os_name", platform.system())
        self.docker_version = self.host_info.get("docker_version", "N/A")

        self.local_ips = self.host_info.get("local_ips", [])
        self.local_ip = next((ip for ip in self.local_ips if not ip.startswith("172.")), self.local_ips[0] if self.local_ips else "")

        # ローカル直接接続 (Active Handshake) 検証状態
        self.is_direct_verified = False
        self.direct_master_url = ""
        self.last_direct_check_time = 0.0
        self.master_instance_id: Optional[str] = None

        self.tunnel_token = os.getenv("CLOUDFLARE_TUNNEL_TOKEN", os.getenv("TUNNEL_TOKEN", "")).strip()
        self.has_tunnel = bool(self.tunnel_token)

        self.current_state = "standby"  # "standby" or "active"
        self.fail_count = 0
        self.last_sync_time = 0.0
        self.last_db_hash: Optional[str] = None
        self.current_code_hash: Optional[str] = None
        self.last_code_update_time = 0.0
        self.running = True

        logger.info(
            f"🆔 HA Watchdog initialized. Node ID: {self.node_id} (Host: {self.hostname} [{self.container_hostname}], "
            f"Spec: {self.cpu_cores} cores / {self.memory_gb} GB RAM / {self.os_name} (Docker: {self.docker_version}), "
            f"Local IP: {self.local_ip or 'None'}, "
            f"Priority: {self.priority}, Failover threshold: {self.fail_threshold} checks = {self.fail_threshold * self.check_interval:.0f}s, "
            f"Tunnel Failover: {'ENABLED' if self.has_tunnel else 'DISABLED'})"
        )

    def _calc_fail_threshold(self, priority: int) -> int:
        """
        Priority ごとのフェイルオーバー無応答回数 (5秒/回) を算出。
        - Priority 1: 6 回 = 30秒 (最速フェイルオーバー)
        - Priority 2: 36 回 = 180秒 (約3分: 先行ノードの起動・Cloudflare Tunnel 切り替え完了を十分待機)
        - Priority 3: 60 回 = 300秒 (約5分)
        - Priority N: 36 + (priority - 2) * 24 回 (約5分 + 2分刻み)
        """
        if priority <= 1:
            return self.base_fail_threshold
        elif priority == 2:
            return 36  # 3分 (180秒)
        else:
            return 36 + (priority - 2) * 24  # Priority 3 は 60回 = 5分 (300秒)

    def _load_cluster_nodes(self) -> List[Dict[str, Any]]:
        """他ノード情報をキャッシュから読込"""
        try:
            if self.cluster_nodes_file.exists():
                return json.loads(self.cluster_nodes_file.read_text(encoding="utf-8"))
        except Exception:
            pass
        return []

    def _save_cluster_nodes(self):
        """他ノード情報をキャッシュへ保存"""
        try:
            self.cluster_nodes_file.parent.mkdir(parents=True, exist_ok=True)
            self.cluster_nodes_file.write_text(json.dumps(self.cluster_nodes, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.debug(f"Failed to save cluster nodes cache: {e}")

    def is_higher_priority_active(self) -> bool:
        """
        自分より優先度の高いノード (Priority 1 など) が Active 稼働しているかを直接相互確認。
        ※ NAT/FW 越しで直接疎通できない環境も多いため、200 OK 応答時のみ「生存」と判定し、
        タイムアウト等は相手ダウンと決めつけず安全にスルーします。
        """
        higher_nodes = [
            n for n in self.cluster_nodes
            if n.get("priority", 999) < self.priority and n.get("ip")
        ]
        if not higher_nodes:
            return False

        for node in higher_nodes:
            ip = node.get("ip")
            port = node.get("port", 8088)
            url = f"http://{ip}:{port}/health"
            try:
                # FW 越え不可を考慮して短時間 (2.0s) でタイムアウト
                req = urllib.request.Request(url, headers={"User-Agent": f"TTSBot-HA-PeerCheck/{self.node_id}"})
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        if data.get("status") == "ok":
                            logger.info(
                                f"🛡️ [Cluster Peer Check] Higher-priority peer '{node.get('hostname')}' "
                                f"({ip}:{port}, Priority {node.get('priority')}) is verified ACTIVE via LAN/Direct connection!"
                            )
                            return True
            except Exception:
                # FW の向こう側で直接アクセスできない環境では正常な挙動のため無視
                pass
        return False

    def _detect_memory_gb(self) -> float:
        """システム物理メモリ (GB) の検出"""
        try:
            if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names and "SC_PHYS_PAGES" in os.sysconf_names:
                total_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
                return round(total_bytes / (1024 ** 3), 1)
        except Exception:
            pass
        try:
            meminfo = Path("/proc/meminfo")
            if meminfo.exists():
                for line in meminfo.read_text().splitlines():
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / (1024 * 1024), 1)
        except Exception:
            pass
        return 0.0

    def _init_node_id(self) -> str:
        """永続的な Node ID を初期化または取得"""
        env_id = os.getenv("TTSBOT_HA_NODE_ID", "").strip()
        if env_id:
            return env_id
        try:
            id_file = self.db_path.parent / "ha_node_id"
            if id_file.exists():
                saved = id_file.read_text(encoding="utf-8").strip()
                if saved:
                    return saved
            clean_host = self.hostname.replace(" ", "_").replace(".", "_")
            new_id = f"slave_{clean_host}_{uuid.uuid4().hex[:8]}"
            id_file.parent.mkdir(parents=True, exist_ok=True)
            id_file.write_text(new_id, encoding="utf-8")
            return new_id
        except Exception:
            return f"slave_{self.hostname}_{uuid.uuid4().hex[:8]}"

    def _get_common_headers(self) -> dict:
        """Master 送信用の認証・ノード識別・スペック・Priority ヘッダー"""
        headers = {
            "User-Agent": f"TTSBot-HA-Watchdog/{self.node_id}",
            "X-HA-Node-ID": self.node_id,
            "X-HA-Hostname": self.hostname,
            "X-HA-Container-Hostname": self.container_hostname,
            "X-HA-Priority": str(self.priority),
            "X-HA-Cpu-Cores": str(self.cpu_cores),
            "X-HA-Memory-Gb": str(self.memory_gb),
            "X-HA-Os": self.os_name,
            "X-HA-Docker-Version": self.docker_version,
            "X-HA-Local-IP": self.local_ip,
            "X-HA-Direct-Verified": "true" if self.is_direct_verified else "false",
            "X-HA-Direct-Url": self.direct_master_url if self.is_direct_verified else "",
        }
        if self.ha_secret:
            headers["Authorization"] = f"Bearer {self.ha_secret}"
            headers["X-HA-Secret"] = self.ha_secret
        return headers

    def verify_master_local_direct(self, master_local_ips: List[str], master_port: int, expected_instance_id: str) -> bool:
        """
        ローカルIPを妄信せず、実際に Master のローカルIPに対して HA 認証付きで接続し、
        master_instance_id の暗号的完全一致を検証する（物理的別サブネット/別拠点の誤認を完全防止）。
        """
        if not master_local_ips or not expected_instance_id:
            self.is_direct_verified = False
            self.direct_master_url = ""
            return False

        now = time.time()
        # 成功時は5分(300秒)毎、未確認時は60秒毎に再検証
        cooldown = 300.0 if self.is_direct_verified else 60.0
        if (now - self.last_direct_check_time) < cooldown:
            return self.is_direct_verified

        self.last_direct_check_time = now

        for ip in master_local_ips:
            if not ip or ip.startswith("127."):
                continue
            candidate_url = f"http://{ip}:{master_port}"
            check_url = f"{candidate_url}/api/ha/status"
            headers = {
                "User-Agent": f"TTSBot-HA-Handshake/{self.node_id}",
                "X-HA-Node-ID": self.node_id,
                "X-HA-Handshake": "direct_verify",
            }
            if self.ha_secret:
                headers["Authorization"] = f"Bearer {self.ha_secret}"
                headers["X-HA-Secret"] = self.ha_secret

            try:
                req = urllib.request.Request(check_url, headers=headers)
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        # 厳格な実体照合: role が master かつ instance_id が完全一致するか確認
                        if data.get("role") == "master" and data.get("master_instance_id") == expected_instance_id:
                            if not self.is_direct_verified or self.direct_master_url != candidate_url:
                                logger.info(
                                    f"🟢 [HA Direct Handshake] Verified LAN Direct connection to Master at {candidate_url} "
                                    f"(Instance ID matched: {expected_instance_id})! Direct sync enabled."
                                )
                            self.is_direct_verified = True
                            self.direct_master_url = candidate_url
                            return True
                        else:
                            logger.warning(
                                f"⚠️ [HA Direct Handshake] IP {ip}:{master_port} responded, but instance ID mismatch! "
                                f"(Expected {expected_instance_id}, got {data.get('master_instance_id')}). "
                                f"Subnet collision or different cluster detected. Ignoring candidate."
                            )
            except Exception as e:
                logger.debug(f"[HA Direct Handshake] Candidate {candidate_url} unreachable: {e}")

        if self.is_direct_verified:
            logger.warning("🟡 [HA Direct Handshake] Direct LAN connection to Master lost. Falling back to WAN URL.")
        self.is_direct_verified = False
        self.direct_master_url = ""
        return False

    def check_internet_connectivity(self) -> bool:
        """外向きインターネット接続 (Cloudflare/Discord) 疎通検証"""
        for host, port in [("discord.com", 443), ("1.1.1.1", 443), ("8.8.8.8", 53)]:
            try:
                s = socket.create_connection((host, port), timeout=3.0)
                s.close()
                return True
            except Exception:
                pass
        return False

    def is_master_healthy(self) -> bool:
        """Master の死活確認 & ハートビート送信 (410 Gone で自律停止)"""
        if not self.master_url:
            return False

        # まず /api/ha/status で認証付き死活確認 ＆ ハートビート送信
        status_url = f"{self.master_url}/api/ha/status"
        req = urllib.request.Request(status_url, headers=self._get_common_headers())
        try:
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    assigned_p = data.get("assigned_priority") or data.get("priority")
                    if assigned_p and isinstance(assigned_p, int) and assigned_p != self.priority:
                        logger.info(f"🔄 [Priority] Master assigned new Priority: {self.priority} -> {assigned_p}")
                        self.priority = assigned_p
                        self.fail_threshold = self._calc_fail_threshold(self.priority)
                        logger.info(f"⏱️ [Failover] New failover threshold: {self.fail_threshold} checks ({self.fail_threshold * self.check_interval:.0f}s)")

                    # Active Handshake: Master のローカルIPに対する暗号的・実体照合直接検証
                    target_instance_id = data.get("master_instance_id")
                    master_local_ips = data.get("master_local_ips", [])
                    master_port = data.get("master_port", 8088)
                    if target_instance_id and master_local_ips:
                        self.master_instance_id = target_instance_id
                        self.verify_master_local_direct(master_local_ips, master_port, target_instance_id)

                    # 他ノード (Peer Standbys) の最新情報をキャッシュ更新
                    peers = data.get("cluster_nodes", [])
                    if isinstance(peers, list):
                        self.cluster_nodes = peers
                        self._save_cluster_nodes()

                    # Master が計画アップデート中の場合、フェイルオーバー判定を一時猶予（fail_count をリセット）
                    if data.get("is_updating") or data.get("updating"):
                        logger.info("⏸️ [HA Watchdog] Master が計画アップデート中 (updating: true) です。フェイルオーバーを猶予待機します。")
                        self.fail_count = 0

                    # Master のコード更新検知 & 自律アップデート
                    master_code_hash = data.get("code_hash")
                    force_update = data.get("force_update", False)
                    if master_code_hash and (self.current_code_hash is None or master_code_hash != self.current_code_hash or force_update):
                        if self.current_code_hash is not None or force_update:
                            logger.info(f"🔄 [HA Watchdog] Master code update detected ({self.current_code_hash[:10] if self.current_code_hash else 'initial'} -> {master_code_hash[:10]}). Triggering auto-update...")
                            self.sync_code_snapshot(master_code_hash)
                        else:
                            self.current_code_hash = master_code_hash

                    return data.get("is_active", True)
                elif resp.status == 410:
                    self._handle_revocation()
                    return False
        except urllib.error.HTTPError as e:
            if e.code == 410:
                self._handle_revocation()
                return False
            logger.debug(f"Master HA status check HTTP {e.code}: {e.reason}")
        except Exception as e:
            logger.debug(f"Master HA status check failed: {e}")

        # フォールバック: 通常の /health
        health_url = f"{self.master_url}/health"
        req_health = urllib.request.Request(health_url, headers=self._get_common_headers())
        try:
            with urllib.request.urlopen(req_health, timeout=4.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    is_ok = data.get("status") == "ok"
                    bot_ready = data.get("bot", {}).get("is_ready", False)
                    return is_ok and bot_ready
                elif resp.status == 410:
                    self._handle_revocation()
                    return False
        except urllib.error.HTTPError as e:
            if e.code == 410:
                self._handle_revocation()
                return False
            logger.debug(f"Master health check failed: {e}")
        except Exception as e:
            logger.debug(f"Master health check failed: {e}")
            return False
        return False

    def _handle_revocation(self):
        """Master からの登録解除・自律停止指令を受信した際の安全停止"""
        logger.critical("🛑 [HA Watchdog] Master から自律停止・登録解除指令 (HTTP 410 Gone / Revoked) を受信しました。")
        logger.critical("🛑 スプリットブレイン (多重昇格) 防止のため、Watchdog を直ちに安全停止します。")
        self.running = False
        sys.exit(0)

    def sync_db_snapshot(self) -> bool:
        """Master の /api/ha/db_snapshot から SQLite バックアップをプル同期 (LAN直通優先 & WAN自動フォールバック)"""
        if not self.master_url or not self.ha_secret:
            return False

        # LAN直通接続が検証済みならローカルURLを優先、未検証またはエラー時は外部マスターURLを使用
        primary_url = self.direct_master_url if (self.is_direct_verified and self.direct_master_url) else self.master_url
        candidate_urls = [primary_url]
        if primary_url != self.master_url:
            candidate_urls.append(self.master_url)

        headers = self._get_common_headers()
        if self.last_db_hash:
            headers["If-None-Match"] = f'"{self.last_db_hash}"'

        for base_url in candidate_urls:
            sync_url = f"{base_url}/api/ha/db_snapshot"
            req = urllib.request.Request(sync_url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=15.0) as resp:
                    if resp.status == 304:
                        logger.debug(f"DB snapshot unchanged (304 Not Modified) via {base_url}.")
                        self.last_sync_time = time.time()
                        return True

                    if resp.status == 410:
                        self._handle_revocation()
                        return False

                    if resp.status != 200:
                        logger.warning(f"DB snapshot sync failed with HTTP {resp.status} via {base_url}")
                        continue

                    compressed = resp.read()
                    raw_bytes = gzip.decompress(compressed)
                    sha256 = hashlib.sha256(raw_bytes).hexdigest()

                    # ハッシュが同じなら書き込みスキップ
                    if sha256 == self.last_db_hash and self.db_path.exists():
                        self.last_sync_time = time.time()
                        return True

                    # 一時ファイルへ保存しアトミック配置
                    self.db_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_target = self.db_path.with_suffix(".db.sync_tmp")
                    tmp_target.write_bytes(raw_bytes)
                    tmp_target.replace(self.db_path)

                    # 単一整合性DBとして読み込ませるため古い WAL/SHM を掃除
                    wal_file = self.db_path.with_name(f"{self.db_path.name}-wal")
                    shm_file = self.db_path.with_name(f"{self.db_path.name}-shm")
                    if wal_file.exists():
                        wal_file.unlink(missing_ok=True)
                    if shm_file.exists():
                        shm_file.unlink(missing_ok=True)

                    self.last_db_hash = sha256
                    self.last_sync_time = time.time()
                    logger.info(f"✨ DB snapshot synchronized successfully via {base_url}! (Size: {len(raw_bytes)} bytes, Hash: {sha256[:10]}...)")
                    return True
            except urllib.error.HTTPError as e:
                if e.code == 304:
                    self.last_sync_time = time.time()
                    return True
                if e.code == 410:
                    self._handle_revocation()
                    return False
                logger.warning(f"DB sync HTTP error via {base_url}: {e.code} {e.reason}")
            except Exception as e:
                logger.warning(f"DB sync error via {base_url}: {e}")
                if base_url == self.direct_master_url and self.is_direct_verified:
                    logger.info("Direct LAN sync failed, will attempt WAN fallback.")
                    self.is_direct_verified = False

        return False

    def sync_code_snapshot(self, master_hash: str) -> bool:
        """Master の /api/ha/code_snapshot から最新ソースコードをプルして自律再起動"""
        if not self.master_url or not self.ha_secret:
            return False

        # 連続更新を防ぐためのクールダウン (30秒)
        now = time.time()
        if now - self.last_code_update_time < 30.0:
            return False

        sync_url = f"{self.master_url}/api/ha/code_snapshot"
        headers = self._get_common_headers()
        if self.current_code_hash:
            headers["If-None-Match"] = f'"{self.current_code_hash}"'

        req = urllib.request.Request(sync_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                if resp.status == 304:
                    self.current_code_hash = master_hash
                    return True

                if resp.status != 200:
                    logger.warning(f"Code snapshot sync failed with HTTP {resp.status}")
                    return False

                compressed = resp.read()
                code_hash = resp.headers.get("X-Code-Hash", master_hash)

                # 解凍先ディレクトリの決定 (コンテナ内 /app/src またはローカル src)
                custom_src = os.getenv("TTSBOT_SRC_DIR", "").strip()
                if custom_src:
                    target_src = Path(custom_src)
                else:
                    target_src = Path(__file__).resolve().parent
                    if target_src.name != "src":
                        target_src = target_src.parent / "src"

                # 壊れたコードによるクラッシュループを防ぐため、まず一時ディレクトリへ安全展開
                with tempfile.TemporaryDirectory() as tmp_dir:
                    tmp_src = Path(tmp_dir) / "src"
                    tmp_src.mkdir(parents=True, exist_ok=True)
                    buf = io.BytesIO(compressed)
                    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
                        for member in tar.getmembers():
                            parts = Path(member.name).parts
                            if parts and parts[0] == "src":
                                rel_path = Path(*parts[1:])
                            else:
                                rel_path = Path(member.name)
                            dest_path = tmp_src / rel_path
                            if ".." in dest_path.parts:
                                continue
                            if member.isdir():
                                dest_path.mkdir(parents=True, exist_ok=True)
                            elif member.isfile():
                                dest_path.parent.mkdir(parents=True, exist_ok=True)
                                f = tar.extractfile(member)
                                if f:
                                    dest_path.write_bytes(f.read())

                    # 展開された全Pythonファイルの構文正当性を検証 (Gate 2)
                    syntax_ok = True
                    for py_f in tmp_src.rglob("*.py"):
                        try:
                            py_compile.compile(str(py_f), doraise=True)
                        except py_compile.PyCompileError as pe:
                            logger.error(f"❌ [HA Watchdog] Downloaded code contains syntax error in {py_f.name}: {pe}. Aborting reload to prevent crash loop!")
                            syntax_ok = False
                            break

                    if not syntax_ok:
                        return False

                    # 構文検証合格: 本番ディレクトリへ配置
                    target_src.mkdir(parents=True, exist_ok=True)
                    for item in tmp_src.rglob("*"):
                        if item.is_file():
                            rel = item.relative_to(tmp_src)
                            dest = target_src / rel
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(item.read_bytes())

                self.current_code_hash = code_hash
                self.last_code_update_time = now
                logger.info(f"✨ [HA Watchdog] Source code verified and synchronized successfully! (Hash: {code_hash[:10]}...)")
                logger.info("🔄 [HA Watchdog] Reloading watchdog process to apply latest code...")

                # プロセス自律リロード (メモリ上書き再起動)
                os.execv(sys.executable, [sys.executable] + sys.argv)
                return True
        except urllib.error.HTTPError as e:
            if e.code == 304:
                self.current_code_hash = master_hash
                self.last_code_update_time = now
                logger.info("✓ [HA Watchdog] Source code is already up-to-date (304 Not Modified).")
                return True
            logger.error(f"Failed to synchronize code snapshot from Master (HTTP {e.code}): {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to synchronize code snapshot from Master: {e}")
            return False

    async def promote_to_active(self):
        """Slave を ACTIVE に昇格 (Docker で app コンテナ、および設定時は tunnel コンテナを起動)"""
        logger.warning("🚨 [FAILOVER] Master outage detected! Promoting Slave to ACTIVE...")

        promoted = False
        target_services = ["app"]
        if self.has_tunnel:
            target_services.append("tunnel")

        # 1. Docker CLI による起動 (--profile active で app および tunnel を一括起動)
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "compose", "--profile", "active", "up", "-d", *target_services,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                logger.info(f"✓ [Docker Compose] {target_services} container(s) started successfully.")
                promoted = True
        except Exception as e:
            logger.debug(f"Docker compose CLI exec failed: {e}")

        # 2. Unix Socket による直接起動 (CLI が無い環境のフォールバック)
        if not promoted and self.docker.is_available():
            containers = self.docker.find_app_containers()
            started_app = False
            for c in containers:
                c_id = c.get("Id", "")
                if self.docker.start_container(c_id):
                    logger.info(f"✓ [Docker Socket] Started app container: {c_id[:12]}")
                    started_app = True

            if self.has_tunnel:
                tunnel_containers = self.docker.find_tunnel_containers()
                for c in tunnel_containers:
                    c_id = c.get("Id", "")
                    if self.docker.start_container(c_id):
                        logger.info(f"✓ [Docker Socket] Started tunnel container: {c_id[:12]}")

            if started_app:
                promoted = True

        if promoted:
            self.current_state = "active"
            logger.info(
                f"🎉 Slave is now ACTIVE! Discord Gateway and VC connections taken over "
                f"(Tunnel: {'ENABLED' if self.has_tunnel else 'DISABLED'})."
            )
        else:
            logger.error("❌ Failed to start app/tunnel containers during promotion!")

    async def demote_to_standby(self):
        """Slave を STANDBY に降格 (Master 復旧時の自動プリエンプト: app および tunnel を停止)"""
        logger.info("✨ [PREEMPT] Master is healthy again (/health 200 OK)! Demoting Slave to STANDBY...")

        demoted = False
        target_services = ["app"]
        if self.has_tunnel:
            target_services.append("tunnel")

        # 1. Docker CLI による停止
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "compose", "--profile", "active", "stop", *target_services,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                logger.info(f"✓ [Docker Compose] {target_services} container(s) stopped successfully.")
                demoted = True
        except Exception as e:
            logger.debug(f"Docker compose CLI stop failed: {e}")

        # 2. Unix Socket による直接停止
        if not demoted and self.docker.is_available():
            containers = self.docker.find_app_containers()
            if self.has_tunnel:
                containers.extend(self.docker.find_tunnel_containers())
            for c in containers:
                c_id = c.get("Id", "")
                if self.docker.stop_container(c_id, timeout_sec=5):
                    logger.info(f"✓ [Docker Socket] Stopped container: {c_id[:12]}")
                    demoted = True

        self.current_state = "standby"
        self.fail_count = 0
        logger.info("💤 Slave is now in STANDBY mode. Monitoring Master...")

    async def run_loop(self):
        """メイン監視・同期ループ"""
        logger.info("======================================================")
        logger.info("  TTSBot Slave (Standby) Watchdog Started")
        logger.info(f"  Master URL     : {self.master_url or '(Not set)'}")
        logger.info(f"  Check Interval : {self.check_interval}s")
        logger.info(f"  Fail Threshold : {self.fail_threshold} consecutive fails")
        logger.info(f"  Sync Interval  : {self.sync_interval}s")
        logger.info(f"  Local DB       : {self.db_path}")
        logger.info("======================================================")

        if not self.master_url:
            logger.error("FATAL: TTSBOT_MASTER_URL is not set. Please specify Master URL.")
            return

        # 初回起動時に即座に初回DBプルを試行
        logger.info("Initiating first DB snapshot synchronization from Master...")
        self.sync_db_snapshot()

        while self.running:
            try:
                loop = asyncio.get_running_loop()
                is_healthy = await loop.run_in_executor(None, self.is_master_healthy)

                if is_healthy:
                    self.fail_count = 0

                    # Master が健全なのに Slave が Active の場合は即座に降格 (Preempt Demote to Master)
                    if self.current_state == "active":
                        await self.demote_to_standby()

                    # 定期 DB 同期
                    now = time.time()
                    if now - self.last_sync_time >= self.sync_interval:
                        await loop.run_in_executor(None, self.sync_db_snapshot)

                else:
                    self.fail_count += 1
                    logger.warning(
                        f"⚠️ Master health check failed ({self.fail_count}/{self.fail_threshold})"
                    )

                    # 【多重昇格調停フェイルセーフ】
                    # 万が一、自身が Active の状態で、より優先度の高い上位ノード (Priority 1 など) も Active になっている場合、
                    # 劣後ノード (Priority 2) である自身を直ちに Standby へ自動降格 (Preempt Demote) してスプリットブレインを解消
                    if self.current_state == "active" and self.priority > 1:
                        is_higher_active = await loop.run_in_executor(None, self.is_higher_priority_active)
                        if is_higher_active:
                            logger.warning(
                                f"🛑 [Split-Brain Resolver] Higher-priority peer is ACTIVE! "
                                f"Demoting this node (Priority {self.priority}) to STANDBY immediately to resolve conflict."
                            )
                            await self.demote_to_standby()
                            continue

                    # 閾値到達 & 現在 Standby ならば昇格判定
                    if self.fail_count >= self.fail_threshold and self.current_state == "standby":
                        # 1. 外向きインターネット疎通の確認
                        has_net = await loop.run_in_executor(None, self.check_internet_connectivity)
                        if not has_net:
                            logger.error(
                                "🚫 Outbound internet connectivity check failed on Slave. "
                                "Suppressing promotion to avoid false failover during local network outage."
                            )
                            continue

                        # 2. Priority > 1 (第2待機系以降) の場合、昇格直前に上位ノード (Priority 1) や Master を直接相互検証
                        if self.priority > 1:
                            logger.info(
                                f"🔍 [Priority {self.priority} Standby] Pre-promotion verification: "
                                "Checking if higher-priority peer (Priority 1) or Master is active..."
                            )
                            is_master_ok = await loop.run_in_executor(None, self.is_master_healthy)
                            is_higher_ok = await loop.run_in_executor(None, self.is_higher_priority_active)
                            if is_master_ok or is_higher_ok:
                                logger.info(
                                    "🛡️ [Split-Brain Guard] Active peer/Master detected! "
                                    "Suppressing promotion to keep this node in standby."
                                )
                                self.fail_count = 0
                                continue

                        await self.promote_to_active()

            except Exception as e:
                logger.error(f"Unexpected error in Watchdog loop: {e}", exc_info=True)

            # ジッター (0.1〜0.8s の揺らぎ) を付与して複数ノードのチェックタイミング同期・衝突を分散
            jitter = random.uniform(0.1, 0.8)
            await asyncio.sleep(self.check_interval + jitter)


def main():
    watchdog = HAWatchdog()
    try:
        asyncio.run(watchdog.run_loop())
    except KeyboardInterrupt:
        logger.info("HA Watchdog received shutdown signal. Exiting.")


if __name__ == "__main__":
    main()
