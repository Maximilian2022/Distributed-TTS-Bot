"""セキュリティ・バリデーション用共通ユーティリティモジュール
SSRF (CWE-918)、パストラバーサル (CWE-22)、XSS (CWE-79) 等の脆弱性を防ぐための防御関数を提供します。
"""
import ipaddress
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)

# 英数字とハイフン・アンダースコアのみ許可する正規表現
_SAFE_IDENTIFIER_RE = re.compile(r'^[a-zA-Z0-9_\-]+$')


def validate_safe_worker_ip(ip_str: str) -> bool:
    """
    通信先IPアドレスが安全かを検証する (SSRF / CWE-918 防護)。
    - 正当な IPv4 / IPv6 アドレスであること
    - ループバック (127.0.0.1, ::1)、リンクローカル/クラウドメタデータ (169.254.169.254)、
      マルチキャスト、未指定 (0.0.0.0)、予約済みアドレスを拒否
    """
    if not ip_str or not isinstance(ip_str, str):
        return False
    ip_clean = ip_str.strip()
    if not re.match(r'^[0-9a-fA-F:\.]+$', ip_clean):
        return False
    try:
        addr = ipaddress.ip_address(ip_clean)
        if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_reserved or addr.is_unspecified:
            logger.warning(f"SSRF guard: Disallowed IP address blocked: {ip_clean}")
            return False
        return True
    except ValueError:
        return False


def validate_safe_path(base_dir: Union[str, Path], subpath: Union[str, Path]) -> Optional[Path]:
    """
    指定されたサブパスがベースディレクトリ配下に安全に収まっているかを検証する (CWE-22 防護)。
    パストラバーサルや不正パスの場合は None を返却。
    """
    try:
        base = Path(base_dir).resolve()
        target = (base / subpath).resolve()
        if target.is_relative_to(base):
            return target
    except Exception as e:
        logger.debug(f"Path validation error: {e}")
    return None


def is_safe_identifier(val: str) -> bool:
    """モデル名、ノードID、ターゲット名などの識別子が安全な英数字・記号のみで構成されているかを検証"""
    if not val or not isinstance(val, str):
        return False
    return bool(_SAFE_IDENTIFIER_RE.match(val))


def safe_json_for_html_script(data: Any) -> str:
    """
    HTML内の <script> タグにインライン展開しても安全な JSON 文字列を生成する (Reflected XSS 防護)。
    HTMLパーサーによる </script> 誤認脱出を防ぐため <, >, & を Unicode エスケープする。
    """
    if data is None:
        return "null"
    raw = json.dumps(data, ensure_ascii=False)
    return (
        raw.replace("<", "\\u003c")
           .replace(">", "\\u003e")
           .replace("&", "\\u0026")
    )
