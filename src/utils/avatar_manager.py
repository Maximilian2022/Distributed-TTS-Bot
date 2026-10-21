"""
アバター画像（AVIF形式）およびギルドメンバー情報のキャッシュマネージャー
Discord CDN からアバターを取得し、ffmpeg (libaom-av1) で 128x128 の超軽量 AVIF 画像に変換して保存・管理する。
"""

import os
import sys
import asyncio
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Optional, Any, Dict, Set

import aiohttp

logger = logging.getLogger("AvatarManager")

# アバターキャッシュの保存ディレクトリ
_DATA_DIR = os.getenv("DATA_DIR", "/app/data" if os.path.exists("/app/data") else "data")
AVATAR_CACHE_DIR = Path(_DATA_DIR) / "avatars"

# 現在ダウンロード・変換中のユーザーIDセット (同一ユーザーの多重リクエストを防止)
_in_flight_user_ids: Set[int] = set()


def get_avatar_dir() -> Path:
    """アバター保存ディレクトリを取得（存在しない場合は自動作成）"""
    AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return AVATAR_CACHE_DIR


def get_avatar_path(user_id: int) -> Path:
    """指定ユーザーの AVIF ファイルパスを取得"""
    return get_avatar_dir() / f"{user_id}.avif"


def has_avatar(user_id: int) -> bool:
    """指定ユーザーのアバターキャッシュが存在し、0バイト超か判定"""
    path = get_avatar_path(user_id)
    return path.exists() and path.stat().st_size > 0


async def convert_image_to_avif(image_bytes: bytes, output_path: Path) -> bool:
    """
    画像バイナリを ffmpeg (libaom-av1) を用いて 128x128 の静止画 AVIF に高速変換して保存。
    書き込み途中の破損を防ぐためアトミックに一時ファイルからリプレイスする。
    """
    temp_in = None
    temp_out = None
    try:
        # 一時入力ファイル（拡張子は汎用）
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f_in:
            f_in.write(image_bytes)
            temp_in = Path(f_in.name)

        # ユニークな一時出力ファイル名 (競合防止)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        unique_token = uuid.uuid4().hex[:8]
        temp_out = output_path.parent / f"{output_path.stem}.{unique_token}.tmp.avif"

        # ffmpeg 実行
        # -vf scale=128:128 (アバターはDiscord側で正方形配信されるため128x128リサイズ)
        # -c:v libaom-av1 -still-picture 1 -crf 32
        cmd = [
            "ffmpeg", "-y",
            "-i", str(temp_in),
            "-vf", "scale=128:128",
            "-frames:v", "1",
            "-c:v", "libaom-av1",
            "-still-picture", "1",
            "-crf", "32",
            str(temp_out)
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace") if stderr else "Unknown error"
            logger.warning(f"ffmpeg AVIF conversion failed (rc={proc.returncode}): {err_msg[:200]}")
            return False

        if not temp_out.exists() or temp_out.stat().st_size == 0:
            logger.warning("ffmpeg AVIF output file is empty or missing")
            return False

        # アトミックに保存先に移動
        os.replace(temp_out, output_path)
        return True

    except Exception as e:
        logger.error(f"Error during AVIF conversion: {e}", exc_info=True)
        return False
    finally:
        # 一時ファイルのクリーンアップ
        if temp_in and temp_in.exists():
            try:
                temp_in.unlink()
            except OSError:
                pass
        if temp_out and temp_out.exists():
            try:
                temp_out.unlink()
            except OSError:
                pass


async def download_and_save_avatar(user_id: int, avatar_url: str) -> bool:
    """
    Discord CDN からアバター画像を非同期ダウンロードし、AVIF に変換してキャッシュ保存。
    """
    if not avatar_url:
        return False

    _in_flight_user_ids.add(user_id)
    out_path = get_avatar_path(user_id)
    try:
        timeout = aiohttp.ClientTimeout(total=10.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(avatar_url) as resp:
                if resp.status != 200:
                    logger.debug(f"Failed to fetch avatar from Discord CDN for user {user_id}: HTTP {resp.status}")
                    return False
                image_bytes = await resp.read()

        if not image_bytes:
            return False

        success = await convert_image_to_avif(image_bytes, out_path)
        if success:
            logger.info(f"Successfully cached avatar for user {user_id} ({out_path.stat().st_size} bytes AVIF)")
        return success

    except Exception as e:
        logger.debug(f"Failed to download/save avatar for user {user_id}: {e}")
        return False
    finally:
        _in_flight_user_ids.discard(user_id)


async def cache_member_info_and_avatar(
    db: Any,
    member_or_user: Any,
    guild_id: Optional[int] = None,
    is_in_guild: bool = True
) -> bool:
    """
    Discordの Member または User オブジェクトから情報を抽出し、
    DBの guild_member_cache と AVIF 画像を安全に更新する。
    ※ Discord REST API は叩かず、渡されたオブジェクトのプロパティのみを使用。
    """
    if not member_or_user:
        return False

    try:
        user_id = member_or_user.id
        gid = guild_id or (getattr(member_or_user, "guild", None).id if getattr(member_or_user, "guild", None) else None)
        if not gid:
            return False

        username = getattr(member_or_user, "name", "") or ""
        display_name = getattr(member_or_user, "display_name", username) or username

        # アバターハッシュの取得
        avatar_obj = getattr(member_or_user, "display_avatar", None) or getattr(member_or_user, "avatar", None)
        avatar_key = avatar_obj.key if avatar_obj and hasattr(avatar_obj, "key") else ""
        avatar_url = avatar_obj.url if avatar_obj and hasattr(avatar_obj, "url") else ""

        # 1. DBキャッシュ更新
        await db.upsert_guild_member_cache(
            guild_id=gid,
            user_id=user_id,
            username=username,
            display_name=display_name,
            avatar_hash=avatar_key,
            is_in_guild=is_in_guild
        )

        # 2. 画像キャッシュ判定
        # アバター画像が存在し、かつ未キャッシュ・未処理中の場合はダウンロード
        need_download = not has_avatar(user_id) and (user_id not in _in_flight_user_ids)
        if need_download and avatar_url:
            _in_flight_user_ids.add(user_id)
            asyncio.create_task(download_and_save_avatar(user_id, str(avatar_url)))

        return True
    except Exception as e:
        logger.debug(f"Failed to cache member info for {getattr(member_or_user, 'id', 'unknown')}: {e}")
        return False
