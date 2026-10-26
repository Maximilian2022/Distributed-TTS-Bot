#!/usr/bin/env python3
"""
TTSBot システム情報・ホスト環境・ネットワーク検出ユーティリティ
Docker コンテナ環境の内外を考慮し、ホストマシンの真のホスト名、OS種類、
Dockerバージョン、およびローカルIP（LAN/プライベートIP）候補を安全・堅牢に取得します。
"""

import os
import sys
import time
import socket
import json
import http.client
import platform
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger("SystemInfo")

# キャッシュ用変数
_system_info_cache: Optional[Dict[str, Any]] = None
_system_info_cache_time: float = 0.0
_CACHE_TTL = 300.0  # 5分間キャッシュ


class DockerUnixSocketClient:
    """Unix Socket (/var/run/docker.sock) 経由で Docker Engine API から情報取得"""

    def __init__(self, socket_path: str = "/var/run/docker.sock"):
        self.socket_path = socket_path

    def is_available(self) -> bool:
        return os.path.exists(self.socket_path)

    def get_json(self, path: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
        if not self.is_available():
            return None
        
        class _UnixConnection(http.client.HTTPConnection):
            def __init__(conn_self, sp):
                super().__init__("localhost", timeout=timeout)
                conn_self.sp = sp
            def connect(conn_self):
                conn_self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn_self.sock.settimeout(timeout)
                conn_self.sock.connect(conn_self.sp)

        conn = None
        try:
            conn = _UnixConnection(self.socket_path)
            conn.request("GET", path)
            resp = conn.getresponse()
            if resp.status == 200:
                raw = resp.read()
                return json.loads(raw.decode("utf-8", errors="replace"))
            return None
        except Exception as e:
            logger.debug(f"[SystemInfo] Docker socket query failed for {path}: {e}")
            return None
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


def get_local_ip_candidates() -> List[str]:
    """
    自ノードのローカルIPv4アドレス候補（プライベートIP）をリストアップ。
    ループバック(127.0.0.1)やリンクローカル(169.254.x.x)は除外。
    """
    candidates = set()

    # 1. 環境変数または .env からの明示指定 (HOST_LOCAL_IP / TTSBOT_LOCAL_IP)
    env_ip = (os.getenv("HOST_LOCAL_IP") or os.getenv("TTSBOT_LOCAL_IP") or "").strip()
    if not env_ip:
        for env_path in [Path("/app/.env"), Path(".env"), Path("/root/ttsbot/.env")]:
            try:
                if env_path.exists():
                    for line in env_path.read_text(encoding="utf-8").splitlines():
                        if line.startswith("HOST_LOCAL_IP=") or line.startswith("TTSBOT_LOCAL_IP="):
                            env_ip = line.split("=", 1)[1].strip().strip('"').strip("'")
                            if env_ip:
                                break
                if env_ip:
                    break
            except Exception:
                pass

    if env_ip:
        candidates.add(env_ip)

    # 2. 外向きダミーUDP接続による主インターフェースIP検出
    for target in [("8.8.8.8", 80), ("1.1.1.1", 80), ("10.255.255.255", 1)]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            s.connect(target)
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                candidates.add(ip)
        except Exception:
            pass

    # 3. getaddrinfo によるホスト解決
    try:
        host_name = socket.gethostname()
        for item in socket.getaddrinfo(host_name, None, socket.AF_INET):
            ip = item[4][0]
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                candidates.add(ip)
    except Exception:
        pass

    return sorted(list(candidates))


def get_host_system_info(force_refresh: bool = False) -> Dict[str, Any]:
    """
    ホストマシンの詳細システム情報を安全に取得。
    - hostname: ホストマシンの真のホスト名 (例: MasterUbuntu)
    - container_hostname: コンテナID (例: f56ecdefd774)
    - os_name: ホストOS名 (例: Ubuntu 26.04.1 LTS)
    - kernel: カーネルバージョン (例: 7.0.0-38-generic)
    - docker_version: Docker Engine バージョン (例: 29.4.2)
    - is_docker: Dockerコンテナ内かどうか
    - local_ips: ローカルIPアドレス候補リスト
    """
    global _system_info_cache, _system_info_cache_time
    now = time.time()
    if not force_refresh and _system_info_cache and (now - _system_info_cache_time) < _CACHE_TTL:
        return dict(_system_info_cache)

    container_id = socket.gethostname()
    is_docker = os.path.exists("/.dockerenv") or os.path.exists("/proc/1/cgroup")

    # 基本フォールバック値
    info: Dict[str, Any] = {
        "hostname": os.getenv("HOST_HOSTNAME", "").strip() or container_id,
        "container_hostname": container_id,
        "os_name": platform.system(),
        "kernel": platform.release(),
        "docker_version": "N/A",
        "is_docker": is_docker,
        "local_ips": get_local_ip_candidates()
    }

    # /etc/os-release からのフォールバックOS取得
    try:
        os_release_path = Path("/etc/os-release")
        if os_release_path.exists():
            for line in os_release_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("PRETTY_NAME="):
                    info["os_name"] = line.split("=", 1)[1].strip('"').strip("'")
                    break
    except Exception:
        pass

    # Docker Engine API からホストの正確な情報を取得
    docker_sock = os.getenv("TTSBOT_DOCKER_SOCKET", "/var/run/docker.sock")
    docker_client = DockerUnixSocketClient(docker_sock)
    if docker_client.is_available():
        # 1. /version -> Dockerバージョン
        v_data = docker_client.get_json("/version")
        if v_data and isinstance(v_data, dict):
            info["docker_version"] = v_data.get("Version", "N/A")

        # 2. /info -> ホスト名、ホストOS、カーネル
        i_data = docker_client.get_json("/info")
        if i_data and isinstance(i_data, dict):
            host_name = i_data.get("Name")
            if host_name:
                info["hostname"] = host_name
            os_name = i_data.get("OperatingSystem")
            if os_name:
                info["os_name"] = os_name
            kernel_ver = i_data.get("KernelVersion")
            if kernel_ver:
                info["kernel"] = kernel_ver

    # ホスト名環境変数のオーバーライド
    if os.getenv("HOST_HOSTNAME"):
        info["hostname"] = os.getenv("HOST_HOSTNAME")

    _system_info_cache = dict(info)
    _system_info_cache_time = now
    return info
