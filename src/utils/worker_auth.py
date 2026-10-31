"""
Master → Worker Agent (:50030) 呼び出し用の認証ヘッダー。
Agent は共有ワーカートークン (X-Worker-Token) を検証するため、Master からの全呼び出しに付与する。
トークンは入れ替え (管理画面のクレデンシャル保存) に追従できるよう、設定オブジェクトから都度読み出す。
"""
from typing import Any, Callable, Dict, Optional

_secret_provider: Optional[Callable[[], str]] = None


def configure_agent_auth(provider: Callable[[], str]) -> None:
    """共有ワーカートークンの取得関数を登録 (WebServer 起動時に設定)"""
    global _secret_provider
    _secret_provider = provider


def agent_auth_headers(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    headers: Dict[str, Any] = dict(extra or {})
    secret = ""
    if _secret_provider is not None:
        try:
            secret = (_secret_provider() or "").strip()
        except Exception:
            secret = ""
    if secret:
        headers["X-Worker-Token"] = secret
    return headers
