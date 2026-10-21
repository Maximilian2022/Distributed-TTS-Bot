import os
import time
import json
import hmac
import hashlib
import base64
import logging
from typing import Optional, Dict, Any, List
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
        bot: discord.Client,
        old_session_secrets: Optional[List[str]] = None
    ):
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.redirect_uri = redirect_uri.strip()
        self.session_secret = session_secret.strip().encode("utf-8")
        self.bot = bot

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

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    def get_login_url(self, state: str = "") -> str:
        """Discord OAuth2 認可URLを生成"""
        from urllib.parse import urlencode
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "identify",
            "prompt": "consent"
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
        if not self.bot.guilds:
            logger.warning("Bot is currently not connected to any guild.")
            return False

        for guild in self.bot.guilds:
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

    def create_session_token(self, user_data: Dict[str, Any], max_age: int = 86400 * 7) -> str:
        """HMAC署名付きセッショントークンを生成"""
        payload = {
            "user_id": int(user_data["id"]),
            "username": user_data.get("username", ""),
            "global_name": user_data.get("global_name") or user_data.get("username", ""),
            "avatar": user_data.get("avatar", ""),
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
