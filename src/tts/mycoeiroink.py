"""
MYCOEIROINK 配布音声情報の取得・キャッシュ管理・安全導入モジュール
COEIROINK 公式サイト (https://coeiroink.com) の公開情報から
有志配布モデル（MYCOE）の一覧・メタデータ・サンプル音声を任意/一括取得し、
自動導入（直リンク）およびWebUI経由のZIP安全アップロード展開をサポートする。
"""

import os
import io
import re
import json
import time
import zipfile
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Set
import aiohttp

logger = logging.getLogger(__name__)

CACHE_FILE_PATH = "/app/data/mycoeiroink_cache.json"
BASE_URL = "https://coeiroink.com"
MAX_UPLOAD_SIZE = 500 * 1024 * 1024  # 500MB
MAX_EXTRACT_SIZE = 1500 * 1024 * 1024  # 1500MB (1.5GB)
MAX_SINGLE_FILE_SIZE = 500 * 1024 * 1024  # 500MB

# セキュリティ対策: 実行可能・スクリプトファイルの拒否リスト
FORBIDDEN_EXTENSIONS = {
    ".exe", ".dll", ".so", ".dylib", ".bin", ".sh", ".bash", ".bat",
    ".cmd", ".ps1", ".vbs", ".py", ".pyc", ".pyd", ".js", ".mjs",
    ".jar", ".elf", ".com", ".scr", ".msi"
}


class MyCoeiroinkManager:
    def __init__(self, cache_path: str = CACHE_FILE_PATH, model_dir: Optional[str] = None):
        self.cache_path = cache_path
        self.model_dir = Path(model_dir or os.getenv("COEIROINK_MODEL_DIR", "/app/coeiroink_models"))
        self._cache_data: Optional[Dict[str, Any]] = None
        self._lock = asyncio.Lock()
        self._load_cache()

    def _load_cache(self) -> None:
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    self._cache_data = json.load(f)
                logger.info(f"Loaded MYCOEIROINK cache from {self.cache_path} ({len(self._cache_data.get('items', []))} items)")
            except Exception as e:
                logger.warning(f"Failed to load MYCOEIROINK cache: {e}")
                self._cache_data = None

    def _save_cache_data(self) -> None:
        if not self._cache_data:
            return
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save MYCOEIROINK cache: {e}")

    def _save_cache(self, items: List[Dict[str, Any]], build_id: str) -> None:
        # 既存キャッシュにある auto_install_failed フラグやカスタム状態を引き継ぐ
        existing_flags = {}
        if self._cache_data and "items" in self._cache_data:
            for old_it in self._cache_data["items"]:
                uuid = old_it.get("speaker_uuid", "").lower()
                if uuid:
                    existing_flags[uuid] = {
                        "auto_install_failed": old_it.get("auto_install_failed", False),
                        "auto_install_msg": old_it.get("auto_install_msg", "")
                    }

        for it in items:
            uuid = it.get("speaker_uuid", "").lower()
            if uuid in existing_flags:
                it["auto_install_failed"] = existing_flags[uuid]["auto_install_failed"]
                it["auto_install_msg"] = existing_flags[uuid]["auto_install_msg"]

        data = {
            "build_id": build_id,
            "last_synced_at": int(time.time()),
            "total_count": len(items),
            "items": items
        }
        self._cache_data = data
        self._save_cache_data()
        logger.info(f"Saved {len(items)} MYCOEIROINK items to cache at {self.cache_path}")

    def is_model_installed(self, speaker_uuid: str) -> bool:
        """指定されたUUIDのモデルがローカルのCOEIROINKモデルディレクトリに存在するか判定"""
        if not self.model_dir.exists():
            return False

        u_target = speaker_uuid.lower().replace("-", "")

        # 1. UUIDそのもののフォルダがあるか
        direct_dir = self.model_dir / speaker_uuid
        if direct_dir.exists() and (direct_dir / "metas.json").exists():
            return True

        # 2. 配下の各フォルダの metas.json を探索
        try:
            for entry in self.model_dir.iterdir():
                if entry.is_dir():
                    meta_path = entry / "metas.json"
                    if meta_path.exists():
                        try:
                            with open(meta_path, "r", encoding="utf-8") as f:
                                meta = json.load(f)
                                m_uuid = str(meta.get("speakerUuid", "")).lower().replace("-", "")
                                if m_uuid == u_target:
                                    return True
                        except Exception:
                            continue
        except Exception as e:
            logger.debug(f"Error scanning model_dir for {speaker_uuid}: {e}")

        return False

    @staticmethod
    def normalize_uuid(uuid_str: str) -> str:
        """UUIDを小文字ハイフン区切り標準形式に正規化"""
        if not uuid_str:
            return ""
        s = uuid_str.strip().lower().replace("-", "")
        if len(s) == 32:
            return f"{s[:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:]}"
        return uuid_str.strip().lower()

    def get_installed_speaker_uuids(self) -> Set[str]:
        """ローカルの COEIROINK モデルディレクトリに存在する全モデルの speakerUuid を取得（正規化済みUUIDの集合）"""
        installed: Set[str] = set()
        if not self.model_dir.exists():
            return installed

        try:
            for entry in self.model_dir.iterdir():
                if entry.is_dir():
                    meta_path = entry / "metas.json"
                    if meta_path.exists():
                        try:
                            with open(meta_path, "r", encoding="utf-8") as f:
                                meta = json.load(f)
                                m_uuid = str(meta.get("speakerUuid", "")).strip()
                                if m_uuid:
                                    installed.add(self.normalize_uuid(m_uuid))
                        except Exception:
                            continue
        except Exception as e:
            logger.warning(f"Error scanning installed speaker uuids: {e}")

        return installed

    async def fetch_build_id(self, session: aiohttp.ClientSession) -> Optional[str]:
        """COEIROINK公式サイトトップからNext.jsのbuildIdを取得"""
        try:
            async with session.get(BASE_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning(f"Failed to fetch {BASE_URL}, status: {resp.status}")
                    return None
                html = await resp.text()

                m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
                if m:
                    return m.group(1)

                m = re.search(r'/_next/static/([^/]+)/_buildManifest\.js', html)
                if m:
                    return m.group(1)
        except Exception as e:
            logger.warning(f"Error fetching COEIROINK buildId: {e}")
        return None

    async def sync_all_from_official(self) -> Dict[str, Any]:
        """公式サイトから最新のMYCOEIROINK配布者リスト全件（全ページ）を一括取得して更新"""
        async with self._lock:
            timeout = aiohttp.ClientTimeout(total=45)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                build_id = await self.fetch_build_id(session)
                if not build_id:
                    if self._cache_data and self._cache_data.get("build_id"):
                        build_id = self._cache_data["build_id"]
                    else:
                        raise RuntimeError("COEIROINK公式サイトからビルドIDを取得できませんでした。")

                first_url = f"{BASE_URL}/_next/data/{build_id}/mycoeiroink/list.json?i=1&sort=new"
                logger.info(f"Fetching MYCOEIROINK page 1 from {first_url}")
                async with session.get(first_url) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"MYCOEIROINK一覧データの取得に失敗しました (HTTP {resp.status})")
                    first_json = await resp.json()

                first_props = first_json.get("pageProps", {})
                max_length = int(first_props.get("maxLength", 1))
                all_distributors = list(first_props.get("distributorInfos", []))

                if max_length > 1:
                    sem = asyncio.Semaphore(4)

                    async def fetch_page(page_idx: int):
                        async with sem:
                            page_url = f"{BASE_URL}/_next/data/{build_id}/mycoeiroink/list.json?i={page_idx}&sort=new"
                            try:
                                async with session.get(page_url) as r:
                                    if r.status == 200:
                                        p_data = await r.json()
                                        return p_data.get("pageProps", {}).get("distributorInfos", [])
                            except Exception as e:
                                logger.warning(f"Failed to fetch MYCOEIROINK page {page_idx}: {e}")
                            return []

                    tasks = [fetch_page(p) for p in range(2, max_length + 1)]
                    results = await asyncio.gather(*tasks)
                    for r in results:
                        all_distributors.extend(r)

                formatted_items = []
                seen_uuids = set()

                for dist in all_distributors:
                    metas = dist.get("metas", {})
                    speaker_name = metas.get("speakerName", "").strip()
                    speaker_uuid = metas.get("speakerUuid", "").strip()
                    if not speaker_name or not speaker_uuid:
                        continue
                    if speaker_uuid.lower() in seen_uuids:
                        continue
                    seen_uuids.add(speaker_uuid.lower())

                    styles = []
                    for st in metas.get("styles", []):
                        styles.append({
                            "style_name": st.get("styleName", ""),
                            "style_id": st.get("styleId", 0)
                        })

                    portrait = dist.get("portraitPath", "")
                    if portrait and not portrait.startswith("http"):
                        portrait_url = f"{BASE_URL}{portrait}"
                    else:
                        portrait_url = portrait

                    audio_samples = {}
                    for style_name, paths in dist.get("audioPathDict", {}).items():
                        full_paths = []
                        for p in paths:
                            if p and not p.startswith("http"):
                                full_paths.append(f"{BASE_URL}{p}")
                            else:
                                full_paths.append(p)
                        audio_samples[style_name] = full_paths

                    formatted_items.append({
                        "speaker_name": speaker_name,
                        "speaker_uuid": speaker_uuid,
                        "kana": metas.get("kana", ""),
                        "url": metas.get("url", ""),
                        "inserted_at": metas.get("inserted_at", ""),
                        "styles": styles,
                        "style_count": len(styles),
                        "portrait_url": portrait_url,
                        "audio_samples": audio_samples,
                        "auto_install_failed": False,
                        "auto_install_msg": ""
                    })

                self._save_cache(formatted_items, build_id)
                logger.info(f"Successfully synced {len(formatted_items)} MYCOEIROINK characters across {max_length} pages.")
                return {
                    "success": True,
                    "total_count": len(formatted_items),
                    "last_synced_at": int(time.time())
                }

    async def get_list(
        self,
        query: str = "",
        page: int = 1,
        limit: int = 30,
        auto_sync_if_empty: bool = True
    ) -> Dict[str, Any]:
        """一覧の取得 (検索・ページネーション・インストール状態付与)"""
        if (not self._cache_data or len(self._cache_data.get("items", [])) == 0) and auto_sync_if_empty:
            try:
                await self.sync_all_from_official()
            except Exception as e:
                logger.error(f"Auto-sync failed: {e}")

        items = (self._cache_data or {}).get("items", [])
        last_synced_at = (self._cache_data or {}).get("last_synced_at", 0)

        q = query.strip().lower()
        if q:
            filtered = []
            for it in items:
                name_match = q in it["speaker_name"].lower()
                kana_match = q in it.get("kana", "").lower()
                uuid_match = q in it["speaker_uuid"].lower()
                style_match = any(q in st["style_name"].lower() for st in it.get("styles", []))
                if name_match or kana_match or uuid_match or style_match:
                    filtered.append(it)
        else:
            filtered = items

        total = len(filtered)
        limit = max(1, min(limit, 100))
        page = max(1, page)
        start_idx = (page - 1) * limit
        end_idx = start_idx + limit
        paged_items = filtered[start_idx:end_idx]

        # インストール状況を動的に判定して付与
        enriched_items = []
        for it in paged_items:
            it_copy = dict(it)
            it_copy["is_installed"] = self.is_model_installed(it["speaker_uuid"])
            # BOOTHやPixiv等、直ダウンロード不可が明らかなドメインは自動判定
            url = (it.get("url") or "").lower()
            if any(dom in url for dom in ["booth.pm", "fanbox.cc", "pixiv.net", "ci-en.dlsite.com"]):
                it_copy["is_manual_source"] = True
            else:
                it_copy["is_manual_source"] = False
            enriched_items.append(it_copy)

        return {
            "items": enriched_items,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": (total + limit - 1) // limit if total > 0 else 1,
            "last_synced_at": last_synced_at
        }

    async def get_speaker_by_uuid(self, speaker_uuid: str) -> Optional[Dict[str, Any]]:
        """指定UUIDのキャラクター情報を取得"""
        items = (self._cache_data or {}).get("items", [])
        for it in items:
            if it["speaker_uuid"].lower() == speaker_uuid.lower():
                return it

        try:
            await self.sync_all_from_official()
            for it in (self._cache_data or {}).get("items", []):
                if it["speaker_uuid"].lower() == speaker_uuid.lower():
                    return it
        except Exception as e:
            logger.warning(f"Failed to find speaker {speaker_uuid} after sync: {e}")

        return None

    def mark_auto_install_failed(self, speaker_uuid: str, reason: str = "") -> None:
        """自動導入失敗フラグをキャッシュに記録"""
        if not self._cache_data or "items" not in self._cache_data:
            return
        for it in self._cache_data["items"]:
            if it.get("speaker_uuid", "").lower() == speaker_uuid.lower():
                it["auto_install_failed"] = True
                it["auto_install_msg"] = reason
                break
        self._save_cache_data()

    def mark_auto_install_success(self, speaker_uuid: str) -> None:
        """自動導入成功時にフラグをリセット"""
        if not self._cache_data or "items" not in self._cache_data:
            return
        for it in self._cache_data["items"]:
            if it.get("speaker_uuid", "").lower() == speaker_uuid.lower():
                it["auto_install_failed"] = False
                it["auto_install_msg"] = ""
                break
        self._save_cache_data()

    def safe_extract_zip(self, zip_data: bytes, speaker_uuid_hint: Optional[str] = None) -> Dict[str, Any]:
        """
        ZIPバイナリを厳格にセキュリティ検証して展開・配置する。
        - Zip Slip 対策 (パストラバーサル防止)
        - 危険拡張子の遮断 (.exe, .sh, .py, .dll 等)
        - サイズ上限チェック (最大 500MB / 展開後最大 1.5GB)
        - metas.json の構造検証
        """
        if len(zip_data) > MAX_UPLOAD_SIZE:
            raise ValueError(f"ZIPファイルサイズが上限 (500MB) を超えています: {len(zip_data) / 1024 / 1024:.1f}MB")

        if not zip_data.startswith(b"PK\x03\x04"):
            raise ValueError("有効なZIPアーカイブではありません（シグネチャ不一致）。")

        self.model_dir.mkdir(parents=True, exist_ok=True)
        resolved_base = self.model_dir.resolve()

        with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
            infolist = z.infolist()
            total_uncompressed = sum(info.file_size for info in infolist)
            if total_uncompressed > MAX_EXTRACT_SIZE:
                raise ValueError(f"展開後の合計サイズが安全上限 (1.5GB) を超えています: {total_uncompressed / 1024 / 1024:.1f}MB")

            # 1. 危険ファイル & パストラバーサル検査 & metas.json 探索
            metas_member: Optional[zipfile.ZipInfo] = None
            metas_prefix = ""

            for info in infolist:
                # 単一ファイルサイズ制限
                if info.file_size > MAX_SINGLE_FILE_SIZE:
                    raise ValueError(f"単一ファイルサイズが異常に巨大です ({info.filename}): {info.file_size / 1024 / 1024:.1f}MB")

                # パストラバーサルチェック
                p = Path(info.filename)
                if p.is_absolute() or ".." in p.parts:
                    raise ValueError(f"パストラバーサルの恐れがあるファイル名です: {info.filename}")

                # 危険拡張子チェック
                ext = p.suffix.lower()
                if ext in FORBIDDEN_EXTENSIONS:
                    raise ValueError(f"セキュリティ上の理由により、実行可能/スクリプト形式のファイルは許可されていません: {info.filename}")

                # metas.json の探索 (ルートまたは第一階層)
                if p.name.lower() == "metas.json":
                    metas_member = info
                    metas_prefix = str(p.parent)
                    if metas_prefix == ".":
                        metas_prefix = ""

            if not metas_member:
                raise ValueError("ZIP内に COEIROINK の設定メタデータ (metas.json) が見つかりませんでした。")

            # 2. metas.json のパースとUUID検証
            try:
                meta_content = z.read(metas_member.filename).decode("utf-8")
                meta_json = json.loads(meta_content)
                meta_uuid = str(meta_json.get("speakerUuid", "")).strip()
                speaker_name = str(meta_json.get("speakerName", "不明")).strip()
                styles = meta_json.get("styles", [])
                if not meta_uuid:
                    raise ValueError("metas.json 内に speakerUuid が存在しません。")
            except Exception as e:
                raise ValueError(f"metas.json の解析に失敗しました: {e}")

            # speaker_uuid_hint が指定されている場合の一致確認 (オプション)
            target_uuid = meta_uuid
            if speaker_uuid_hint and speaker_uuid_hint.lower().replace("-", "") != meta_uuid.lower().replace("-", ""):
                logger.warning(f"Uploaded speakerUuid ({meta_uuid}) differs from hint ({speaker_uuid_hint})")

            # 3. 展開先ディレクトリの確定 (/app/coeiroink_models/<speaker_uuid>/)
            target_speaker_dir = resolved_base / target_uuid
            target_speaker_dir.mkdir(parents=True, exist_ok=True)
            target_speaker_resolved = target_speaker_dir.resolve()

            # 4. 各ファイルの安全な展開
            extracted_files_count = 0
            for info in infolist:
                rel_filename = info.filename
                # metas.json が入っていたサブフォルダ階層をストリップ
                if metas_prefix:
                    if rel_filename.startswith(metas_prefix + "/") or rel_filename.startswith(metas_prefix + "\\"):
                        rel_filename = rel_filename[len(metas_prefix) + 1:]
                    elif rel_filename == metas_prefix:
                        continue
                    else:
                        # 共通ルート外の不要ファイルはスキップ
                        continue

                if not rel_filename or rel_filename.endswith("/"):
                    continue

                dest_path = (target_speaker_resolved / rel_filename).resolve()
                # Zip Slip 最終防壁
                if not dest_path.is_relative_to(target_speaker_resolved):
                    raise ValueError(f"展開先がモデルディレクトリ外です: {dest_path}")

                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with open(dest_path, "wb") as f_out:
                    f_out.write(z.read(info.filename))
                extracted_files_count += 1

            self.mark_auto_install_success(target_uuid)
            logger.info(f"Successfully extracted COEIROINK model '{speaker_name}' ({target_uuid}) to {target_speaker_dir} ({extracted_files_count} files)")

            return {
                "success": True,
                "speaker_name": speaker_name,
                "speaker_uuid": target_uuid,
                "styles_count": len(styles),
                "extracted_files": extracted_files_count,
                "message": f"「{speaker_name}」の導入が正常に完了しました！"
            }

    async def try_auto_install(self, speaker_uuid: str) -> Dict[str, Any]:
        """
        指定されたキャラクターの配布URLから自動ダウンロード・導入を試みる。
        直リンク等でなければ失敗フラグを立てて手動案内を返す。
        """
        sp = await self.get_speaker_by_uuid(speaker_uuid)
        if not sp:
            return {
                "success": False,
                "requires_manual": True,
                "message": "キャラクター情報が見つかりませんでした。"
            }

        speaker_name = sp.get("speaker_name", "不明")
        raw_url = (sp.get("url") or "").strip()

        if not raw_url:
            self.mark_auto_install_failed(speaker_uuid, "配布URLが登録されていません。")
            return {
                "success": False,
                "requires_manual": True,
                "message": "配布URLが登録されていません。"
            }

        # ログインやカートが必要なWebサイトの場合は即座に手動ダウンロードへ切り替え
        lower_url = raw_url.lower()
        if any(dom in lower_url for dom in ["booth.pm", "fanbox.cc", "pixiv.net", "ci-en.dlsite.com"]):
            self.mark_auto_install_failed(speaker_uuid, "BOOTHまたは外部サイトでの購入・手動ダウンロードが必要です。")
            return {
                "success": False,
                "requires_manual": True,
                "source_url": raw_url,
                "message": "BOOTHまたは外部配布ページからの手動ダウンロードが必要です。"
            }

        # Google Drive の共有リンクをダイレクトダウンロードURLに変換試行
        dl_url = raw_url
        if "drive.google.com" in lower_url:
            m = re.search(r"/d/([a-zA-Z0-9_-]+)", raw_url)
            if not m:
                m = re.search(r"id=([a-zA-Z0-9_-]+)", raw_url)
            if m:
                file_id = m.group(1)
                dl_url = f"https://drive.google.com/uc?export=download&id={file_id}"

        # Dropbox の共有リンクを直リンク化
        if "dropbox.com" in lower_url:
            if "dl=0" in dl_url:
                dl_url = dl_url.replace("dl=0", "dl=1")
            elif "?" not in dl_url:
                dl_url += "?dl=1"

        logger.info(f"Attempting auto-download for {speaker_name} from: {dl_url}")

        try:
            timeout = aiohttp.ClientTimeout(total=90)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                async with session.get(dl_url, allow_redirects=True) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTPエラー {resp.status} が返されました。")

                    # Content-Type または 最初のヘッダーで HTML 判定
                    content_type = resp.headers.get("Content-Type", "").lower()
                    if "text/html" in content_type:
                        raise RuntimeError("ダウンロードリンクではなくWebページ（HTML）が返却されました。")

                    # ストリーミングダウンロード（最大 500MB）
                    chunks = []
                    downloaded = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        downloaded += len(chunk)
                        if downloaded > MAX_UPLOAD_SIZE:
                            raise RuntimeError("ファイルサイズが上限 (500MB) を超過しました。")
                        chunks.append(chunk)

                    zip_data = b"".join(chunks)

            # ZIPのシグネチャチェック
            if not zip_data.startswith(b"PK\x03\x04"):
                raise RuntimeError("ダウンロードされたデータが有効なZIPファイルではありません。")

            # 安全解凍
            result = self.safe_extract_zip(zip_data, speaker_uuid)
            return {
                "success": True,
                "speaker_name": speaker_name,
                "speaker_uuid": speaker_uuid,
                "message": f"「{speaker_name}」の自動導入に成功しました！"
            }

        except Exception as e:
            logger.warning(f"Auto-install failed for {speaker_name} ({speaker_uuid}): {e}")
            self.mark_auto_install_failed(speaker_uuid, str(e))
            return {
                "success": False,
                "requires_manual": True,
                "source_url": raw_url,
                "message": f"自動導入に失敗しました ({e})。手動でZIPをダウンロードしてアップロードしてください。"
            }
