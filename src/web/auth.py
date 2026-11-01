import os
import time
import json
import hmac
import hashlib
import base64
import secrets
import logging
from urllib.parse import urlencode
from typing import Optional, Dict, Any, List, Callable, Tuple
import aiohttp
from aiohttp import web
import discord

logger = logging.getLogger(__name__)

DISCORD_API_ENDPOINT = "https://discord.com/api/v10"

class DiscordAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        session_secret: str,
        bot: Optional[discord.Client] = None,
        old_session_secrets: Optional[List[str]] = None,
        bot_provider: Optional[Callable[[], Any]] = None
    ):
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.redirect_uri = redirect_uri.strip()
        self.session_secret = session_secret.strip().encode("utf-8")
        self.bot = bot
        # Bot は WebServer 起動後 (BotPool 経由) に生成されるため、判定時に遅延解決するプロバイダー
        self.bot_provider = bot_provider

        # シークレットローテーション用: 旧シークレットリスト
        self.old_session_secrets: List[bytes] = []
        if old_session_secrets:
            for s in old_session_secrets:
                if s and s.strip():
                    self.old_session_secrets.append(s.strip().encode("utf-8"))

        # 環境変数からの旧シークレット自動ロード (カンマ区切り対応)
        env_old_secrets = os.getenv("WEB_SESSION_SECRETS_PREVIOUS", os.getenv("WEB_SESSION_OLD_SECRETS", ""))
        if env_old_secrets:
            for s in env_old_secrets.split(","):
                s_strip = s.strip().encode("utf-8")
                if s_strip and s_strip not in self.old_session_secrets and s_strip != self.session_secret:
                    self.old_session_secrets.append(s_strip)

        # マジックリンク (ワンタイムURL) ノンス管理 (nonce -> expire_timestamp)
        self._consumed_magic_nonces: Dict[str, float] = {}

        # Discord アプリワンタップ承認ログイン管理 (req_id -> dict)
        self.pending_app_logins: Dict[str, Dict[str, Any]] = {}
        self.ip_rate_limits: Dict[str, List[float]] = {}
        self.user_app_login_cooldowns: Dict[int, float] = {}

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    def get_login_url(self, state: str = "", prompt: str = "none") -> str:
        """Discord OAuth2 認可URLを生成 (prompt='none' で承認済み環境なら0クリック自動通過)"""
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "identify",
            "prompt": prompt
        }
        if state:
            params["state"] = state
        return f"https://discord.com/oauth2/authorize?{urlencode(params)}"

    async def exchange_code(self, code: str) -> Optional[str]:
        """認可コードをアクセストークンと交換"""
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async with aiohttp.ClientSession() as session:
            async with session.post(f"{DISCORD_API_ENDPOINT}/oauth2/token", data=data, headers=headers) as resp:
                if resp.status == 200:
                    token_data = await resp.json()
                    return token_data.get("access_token")
                else:
                    text = await resp.text()
                    logger.error(f"Failed to exchange OAuth2 code: HTTP {resp.status} - {text}")
                    return None

    async def fetch_user_info(self, access_token: str) -> Optional[Dict[str, Any]]:
        """アクセストークンからDiscordユーザー情報を取得"""
        headers = {"Authorization": f"Bearer {access_token}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{DISCORD_API_ENDPOINT}/users/@me", headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    text = await resp.text()
                    logger.error(f"Failed to fetch user info: HTTP {resp.status} - {text}")
                    return None

    async def is_user_in_bot_guilds(self, user_id: int) -> bool:
        """
        ユーザーがBotの参加しているいずれかのサーバーに所属しているか検証
        """
        bot = self.bot
        if self.bot_provider is not None:
            try:
                bot = self.bot_provider() or bot
            except Exception as e:
                logger.debug(f"bot_provider resolution failed: {e}")

        if not bot or not getattr(bot, "guilds", None):
            logger.warning("Bot is currently not connected to any guild.")
            return False

        for guild in bot.guilds:
            member = guild.get_member(user_id)
            if member is not None:
                return True
            try:
                member = await guild.fetch_member(user_id)
                if member is not None:
                    return True
            except Exception:
                continue

        return False

    def create_session_token(self, user_data: Dict[str, Any], max_age: int = 86400 * 30, is_admin: bool = False) -> str:
        """HMAC署名付きセッショントークンを生成 (デフォルト30日間有効)"""
        payload = {
            "user_id": int(user_data.get("id") or user_data.get("user_id", 0)),
            "username": user_data.get("username", "Admin"),
            "global_name": user_data.get("global_name") or user_data.get("username", "Admin"),
            "avatar": user_data.get("avatar", ""),
            "is_admin": bool(is_admin or user_data.get("is_admin", False)),
            "exp": int(time.time()) + max_age
        }
        raw_json = json.dumps(payload, separators=(',', ':'))
        b64_payload = base64.urlsafe_b64encode(raw_json.encode()).decode()
        signature = hmac.new(self.session_secret, b64_payload.encode(), hashlib.sha256).hexdigest()
        return f"{b64_payload}.{signature}"

    def verify_session_token(self, token: str) -> Optional[Dict[str, Any]]:
        """HMAC署名付きセッショントークンを検証・復号 (旧シークレットへのフォールバック対応)"""
        if not token or "." not in token:
            return None

        parts = token.split(".", 1)
        if len(parts) != 2:
            return None

        b64_payload, signature = parts

        # 1. 現在の最新シークレットで検証
        expected_sig = hmac.new(self.session_secret, b64_payload.encode(), hashlib.sha256).hexdigest()
        is_valid = hmac.compare_digest(signature, expected_sig)

        # 2. 一致しない場合、旧シークレット群でフォールバック検証 (シークレットローテーション対応)
        if not is_valid and self.old_session_secrets:
            for old_secret in self.old_session_secrets:
                alt_sig = hmac.new(old_secret, b64_payload.encode(), hashlib.sha256).hexdigest()
                if hmac.compare_digest(signature, alt_sig):
                    is_valid = True
                    logger.debug("Session token verified using legacy session secret.")
                    break

        if not is_valid:
            logger.warning("Session token HMAC verification failed.")
            return None

        try:
            raw_json = base64.urlsafe_b64decode(b64_payload.encode()).decode()
            payload = json.loads(raw_json)

            if payload.get("exp", 0) < time.time():
                logger.debug("Session token has expired.")
                return None

            return payload
        except Exception as e:
            logger.warning(f"Error decoding session token: {e}")
            return None

    def get_current_user(self, request: web.Request) -> Optional[Dict[str, Any]]:
        """リクエストのCookieから認証済みユーザー情報を取得"""
        cookie = request.cookies.get("ttsbot_session")
        if not cookie:
            return None
        return self.verify_session_token(cookie)

    # ==========================================================================
    # 【A案】本人専用ワンタイム・マジックリンク (HMAC署名 + Nonce単一消費)
    # ==========================================================================
    def create_magic_login_token(
        self,
        user_id: int,
        username: str,
        is_admin: bool = False,
        target_path: str = "/",
        expires_in: int = 600
    ) -> str:
        """Discordのボタン押下時等に本人専用の短命ワンタイムログインURLトークンを発行 (有効期限10分)"""
        self._clean_magic_nonces()
        nonce = secrets.token_urlsafe(16)
        exp_ts = int(time.time()) + expires_in
        payload = {
            "type": "magic_login",
            "user_id": int(user_id),
            "username": str(username),
            "is_admin": bool(is_admin),
            "path": str(target_path) if target_path.startswith("/") else "/",
            "exp": exp_ts,
            "nonce": nonce
        }
        raw_json = json.dumps(payload, separators=(',', ':'))
        b64_payload = base64.urlsafe_b64encode(raw_json.encode()).decode()
        sig = hmac.new(self.session_secret, b64_payload.encode(), hashlib.sha256).hexdigest()
        return f"mgl_{b64_payload}.{sig}"

    def verify_and_consume_magic_login_token(self, token: str) -> Optional[Dict[str, Any]]:
        """マジックリンクトークンを検証し、一度限りで消費 (リプレイアタック完全抑止)"""
        self._clean_magic_nonces()
        if not token or not token.startswith("mgl_") or "." not in token:
            return None

        raw_token = token[4:]
        parts = raw_token.split(".", 1)
        if len(parts) != 2:
            return None

        b64_payload, signature = parts
        expected_sig = hmac.new(self.session_secret, b64_payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            # 旧シークレットでも検証
            is_valid = False
            for old_sec in self.old_session_secrets:
                alt_sig = hmac.new(old_sec, b64_payload.encode(), hashlib.sha256).hexdigest()
                if hmac.compare_digest(signature, alt_sig):
                    is_valid = True
                    break
            if not is_valid:
                logger.warning("Magic login token HMAC verification failed.")
                return None

        try:
            raw_json = base64.urlsafe_b64decode(b64_payload.encode()).decode()
            payload = json.loads(raw_json)

            if payload.get("type") != "magic_login":
                return None
            if payload.get("exp", 0) < time.time():
                logger.info("Magic login token has expired.")
                return None

            nonce = payload.get("nonce")
            if not nonce or nonce in self._consumed_magic_nonces:
                logger.warning("Magic login token nonce already consumed or invalid.")
                return None

            # ノンスを消費済みとしてマーク (有効期限まで保持)
            self._consumed_magic_nonces[nonce] = payload.get("exp", time.time() + 600)
            return payload
        except Exception as e:
            logger.warning(f"Error decoding magic login token: {e}")
            return None

    def _clean_magic_nonces(self) -> None:
        """期限切れの消費済みノンスを定期クリーンアップ"""
        now = time.time()
        expired = [n for n, exp in self._consumed_magic_nonces.items() if exp < now]
        for n in expired:
            self._consumed_magic_nonces.pop(n, None)

    # ==========================================================================
    # 【B案】Discordアプリ ワンタップ承認ログイン管理 (レートリミット + ポーリング)
    # ==========================================================================
    def check_ip_rate_limit(self, ip: str, max_requests: int = 3, window_sec: float = 60.0) -> bool:
        """IPアドレス単位の承認リクエスト頻度制限 (1分間に3回まで)"""
        now = time.time()
        history = self.ip_rate_limits.get(ip, [])
        # ウィンドウ外の履歴を削除
        valid_history = [t for t in history if now - t < window_sec]
        if len(valid_history) >= max_requests:
            self.ip_rate_limits[ip] = valid_history
            return False
        valid_history.append(now)
        self.ip_rate_limits[ip] = valid_history
        return True

    def can_request_app_login_for_user(self, user_id: int, cooldown_sec: float = 60.0) -> bool:
        """特定ユーザーに対する連続承認リクエスト抑止 (クールダウン60秒)"""
        now = time.time()
        last_req = self.user_app_login_cooldowns.get(user_id, 0.0)
        return (now - last_req) >= cooldown_sec

    def create_app_login_request(
        self,
        user_id: int,
        username: str,
        ip: str,
        user_agent: str,
        expires_in: int = 120
    ) -> Tuple[str, str]:
        """Discordアプリ承認ログイン要求を生成 (有効期限120秒、番号一致認証コード付与)"""
        now = time.time()
        self._clean_app_login_requests()
        req_id = secrets.token_urlsafe(24)
        # 10〜99の2桁のランダム確認番号を生成 (Number Matching用)
        verify_code = str(secrets.randbelow(90) + 10)
        self.pending_app_logins[req_id] = {
            "req_id": req_id,
            "user_id": int(user_id),
            "username": str(username),
            "verify_code": verify_code,
            "ip": str(ip),
            "user_agent": str(user_agent)[:120],
            "status": "pending",  # pending, approved, rejected, expired
            "created_at": now,
            "expires_at": now + expires_in,
            "session_token": None
        }
        self.user_app_login_cooldowns[user_id] = now
        return req_id, verify_code

    def approve_app_login_request(self, req_id: str, session_token: str, verify_code: Optional[str] = None) -> bool:
        """ユーザーがDiscord側で承認ボタンを押した際の確定処理 (番号一致照合付き)"""
        req = self.pending_app_logins.get(req_id)
        if not req or req.get("status") != "pending" or req.get("expires_at", 0) < time.time():
            return False
        # 番号確認がある場合は照合
        if verify_code is not None and str(req.get("verify_code")) != str(verify_code):
            logger.warning(f"App login approval failed: verify code mismatch (expected {req.get('verify_code')}, got {verify_code})")
            return False
        req["status"] = "approved"
        req["session_token"] = session_token
        return True

    def reject_app_login_request(self, req_id: str) -> bool:
        """ユーザーがDiscord側で拒否ボタンを押した際の処理"""
        req = self.pending_app_logins.get(req_id)
        if not req or req.get("status") != "pending":
            return False
        req["status"] = "rejected"
        return True

    def get_app_login_status(self, req_id: str) -> Dict[str, Any]:
        """Webフロントエンドからの承認状態ポーリング確認"""
        self._clean_app_login_requests()
        req = self.pending_app_logins.get(req_id)
        if not req:
            return {"status": "not_found"}
        if req.get("expires_at", 0) < time.time():
            req["status"] = "expired"
        return {
            "status": req.get("status", "expired"),
            "session_token": req.get("session_token") if req.get("status") == "approved" else None,
            "expires_in": max(0, int(req.get("expires_at", 0) - time.time()))
        }

    def _clean_app_login_requests(self) -> None:
        """期限切れの承認リクエストを掃除"""
        now = time.time()
        expired = [r_id for r_id, r in self.pending_app_logins.items() if r.get("expires_at", 0) < now - 300]
        for r_id in expired:
            self.pending_app_logins.pop(r_id, None)
