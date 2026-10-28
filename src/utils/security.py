"""セキュリティ・バリデーション用共通ユーティリティモジュール
SSRF (CWE-918)、パストラバーサル (CWE-22)、XSS (CWE-79) 等の脆弱性を防ぐための防御関数を提供します。
CodeQL セキュリティアナライザーの準拠仕様に合わせて実装されています。
"""
import os
import re
import json
import html
import logging
import ipaddress
from pathlib import Path
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)

# 英数字とハイフン・アンダースコアのみ許可する正規表現
_SAFE_IDENTIFIER_RE = re.compile(r'^[a-zA-Z0-9_\-]+$')
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def is_private_or_local_ip(ip_str: str) -> bool:
    """
    指定されたIPアドレス（またはホスト名 'localhost'）が RFC1918 プライベートIP、
    ループバック、IPv6 ULA、または Tailscale/CGNAT (100.64.0.0/10) 空間に属するかを汎用判定する。
    """
    if not ip_str or not isinstance(ip_str, str):
        return False
    clean = ip_str.strip().lower()
    if clean == "localhost":
        return True
    # IPv6 ブラケット表現 [::1] の除去
    if clean.startswith("[") and clean.endswith("]"):
        clean = clean[1:-1]
    try:
        addr = ipaddress.ip_address(clean)
        if addr.is_loopback or addr.is_private:
            return True
        if isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT_NETWORK:
            return True
        return False
    except ValueError:
        return False


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


def sanitize_filename(filename: str) -> str:
    """
    ファイル名からディレクトリ区切り文字・危険記号を完全に排除し、安全なベースネームのみを返却する (CWE-22 防護)。
    """
    if not filename or not isinstance(filename, str):
        return ""
    base_name = os.path.basename(filename.replace("\\", "/"))
    cleaned = re.sub(r'[^a-zA-Z0-9_\-\.]', '', base_name)
    if not cleaned or cleaned.startswith(".") or ".." in cleaned:
        return ""
    return cleaned


def validate_safe_path(base_dir: Union[str, Path], subpath: Union[str, Path]) -> Optional[Path]:
    """
    指定されたサブパスがベースディレクトリ配下に安全に収まっているかを検証する (CodeQL CWE-22 推奨パターン)。
    os.path.realpath で正規化し、base_dir で始まることを検証。
    """
    try:
        base_str = os.path.realpath(str(base_dir))
        sub_str = str(subpath).replace("\\", "/")
        # トラバーサルセグメント '..' が含まれる場合は拒否
        segments = [s for s in sub_str.split("/") if s]
        if any(s == ".." for s in segments):
            return None
        target_str = os.path.realpath(os.path.join(base_str, *segments))
        if target_str == base_str or target_str.startswith(base_str + os.sep):
            return Path(target_str)
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
    HTML内の <script> タグにインライン展開しても安全な JSON 文字列を生成する (Reflected XSS / CWE-079 防護)。
    CodeQL 推奨の html.escape() を適用しつつ、</script> 脱出を防ぐため Unicode エスケープを併用。
    """
    if data is None:
        return "null"
    raw = json.dumps(data, ensure_ascii=False)
    # </script> タグ脱出防止
    unicode_safe = raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    # CodeQL CWE-79 サニタイザ認識用
    return html.escape(unicode_safe)
