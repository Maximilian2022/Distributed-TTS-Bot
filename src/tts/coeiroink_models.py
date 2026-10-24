import os
import io
import time
import json
import logging
import asyncio
import zipfile
import urllib.request
from pathlib import Path
from typing import Dict, List, Any, Optional, Callable

logger = logging.getLogger(__name__)

# フォールバック用の公式公認スピーカーデータ (14キャラクター、78スタイル)
OFFICIAL_SPEAKERS_FALLBACK_FILE = Path(__file__).resolve().parent / "coeiroink_official_speakers.json"

class CoeiroinkModelManager:
    """
    COEIROINK v2 の音声モデル（公認キャラクター・スタイル）の管理・ダウンロード・更新を行うクラス。
    標準ライブラリ (urllib, zipfile) を使用してホスト・コンテナ問わず動作可能。
    """
    def __init__(self, model_dir: Optional[str] = None):
        self.model_dir = Path(model_dir or os.getenv("COEIROINK_MODEL_DIR", "/app/coeiroink_models"))
        self._lock = asyncio.Lock()
        self.is_downloading = False
        self.progress: Dict[str, Any] = {
            "is_running": False,
            "current_speaker": "",
            "current_style": "",
            "completed": 0,
            "total": 0,
            "percent": 0,
            "message": "",
            "error": None
        }
        self._official_speakers_cache: Optional[List[Dict[str, Any]]] = None

    def get_official_speakers(self) -> List[Dict[str, Any]]:
        """公認キャラクターのメタデータ定義を取得"""
        if self._official_speakers_cache:
            return self._official_speakers_cache

        if OFFICIAL_SPEAKERS_FALLBACK_FILE.exists():
            try:
                with open(OFFICIAL_SPEAKERS_FALLBACK_FILE, "r", encoding="utf-8") as f:
                    self._official_speakers_cache = json.load(f)
                    return self._official_speakers_cache
            except Exception as e:
                logger.error(f"Failed to load fallback official speakers: {e}")

        return []

    def fetch_latest_official_speakers_sync(self, force: bool = False) -> List[Dict[str, Any]]:
        """
        COEIROINK公式サイトのヘルプページから最新の公認キャラクター定義をフェッチ (同期)
        過剰アクセスを完全に防ぐため、前回の取得から30日 (月1回) 以上経過していない場合はローカル定義を返却。
        """
        data_dir = Path("/app/data") if Path("/app/data").exists() else Path("data")
        check_file = data_dir / "coeiroink_last_check.json"
        now = time.time()
        if not force and check_file.exists():
            try:
                with open(check_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    last_ts = data.get("last_checked_at", 0)
                    # 30日 (2,592,000秒) 以内なら外部アクセスをスキップ
                    if now - last_ts < 30 * 86400:
                        logger.debug(f"COEIROINK official speakers check skipped (checked {int((now - last_ts) / 86400)} days ago, monthly rate limit).")
                        return self.get_official_speakers()
            except Exception:
                pass

        url = "https://coeiroink.com/help/016"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    html = resp.read().decode("utf-8", errors="replace")
                    import re
                    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
                    if m:
                        data = json.loads(m.group(1))
                        speakers = data.get("props", {}).get("pageProps", {}).get("downloadableSpeakers", [])
                        cleaned = []
                        for s in speakers:
                            s_clean = {
                                "speakerName": s["speakerName"],
                                "speakerUuid": s["speakerUuid"],
                                "version": s["version"],
                                "prefix": s["prefix"],
                                "metaDownloadUrl": s["metaDownloadUrl"],
                                "styles": [
                                    {
                                        "styleName": st["styleName"],
                                        "styleId": st["styleId"],
                                        "version": st.get("version", "1.0.0"),
                                        "downloadUrl": st["downloadUrl"]
                                    }
                                    for st in s.get("styles", [])
                                ]
                            }
                            cleaned.append(s_clean)
                        if cleaned:
                            self._official_speakers_cache = cleaned
                            try:
                                with open(OFFICIAL_SPEAKERS_FALLBACK_FILE, "w", encoding="utf-8") as f:
                                    json.dump(cleaned, f, ensure_ascii=False, indent=2)
                                check_file.parent.mkdir(parents=True, exist_ok=True)
                                with open(check_file, "w", encoding="utf-8") as f:
                                    json.dump({"last_checked_at": now}, f)
                            except Exception:
                                pass
                            return cleaned
        except Exception as e:
            logger.warning(f"Could not fetch latest COEIROINK official speakers from web: {e}")

        return self.get_official_speakers()

    async def fetch_latest_official_speakers(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.fetch_latest_official_speakers_sync)

    def get_status(self) -> Dict[str, Any]:
        """
        ローカルのインストール状況を走査し、公認キャラクターの導入状況とサマリーを返却。
        """
        official = self.get_official_speakers()
        speakers_status = []
        total_official_styles = 0
        installed_official_styles = 0
        installed_official_speakers = 0

        self.model_dir.mkdir(parents=True, exist_ok=True)

        for sp in official:
            prefix = sp["prefix"]
            ver = sp["version"]
            expected_dir_name = f"{prefix}-{ver}"
            sp_dir = self.model_dir / expected_dir_name

            if not sp_dir.exists():
                matching_dirs = [d for d in self.model_dir.iterdir() if d.is_dir() and d.name.startswith(prefix)]
                if matching_dirs:
                    sp_dir = matching_dirs[0]

            has_meta = (sp_dir / "metas.json").exists()
            model_dir = sp_dir / "model"

            styles_info = []
            sp_installed_styles_cnt = 0

            for st in sp.get("styles", []):
                style_id = st["styleId"]
                total_official_styles += 1

                style_dir = model_dir / str(style_id)
                has_model = False
                if style_dir.exists() and style_dir.is_dir():
                    has_model = any(f.name.endswith(".pth") for f in style_dir.iterdir() if f.is_file())

                if has_model:
                    sp_installed_styles_cnt += 1
                    installed_official_styles += 1

                styles_info.append({
                    "styleId": style_id,
                    "styleName": st["styleName"],
                    "version": st.get("version", "1.0.0"),
                    "is_installed": has_model
                })

            is_sp_installed = (has_meta and sp_installed_styles_cnt > 0)
            is_sp_complete = (has_meta and sp_installed_styles_cnt == len(sp.get("styles", [])))
            if is_sp_installed:
                installed_official_speakers += 1

            speakers_status.append({
                "speakerName": sp["speakerName"],
                "speakerUuid": sp["speakerUuid"],
                "version": sp["version"],
                "prefix": sp["prefix"],
                "is_installed": is_sp_installed,
                "is_complete": is_sp_complete,
                "installed_styles_count": sp_installed_styles_cnt,
                "total_styles_count": len(sp.get("styles", [])),
                "styles": styles_info
            })

        return {
            "model_dir": str(self.model_dir),
            "speakers": speakers_status,
            "summary": {
                "total_speakers": len(official),
                "installed_speakers": installed_official_speakers,
                "total_styles": total_official_styles,
                "installed_styles": installed_official_styles,
                "is_all_installed": (installed_official_styles == total_official_styles and total_official_styles > 0)
            },
            "progress": self.progress
        }

    def _download_and_extract_zip(self, url: str, extract_handler: Callable[[zipfile.ZipFile], None]):
        """URLからストリーミングダウンロードしてZIP展開"""
        if not (url.startswith("https://") or url.startswith("http://")):
            raise ValueError(f"Insecure or invalid URL scheme: {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = resp.read()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                extract_handler(z)

    def download_speaker_sync(self, prefix: str) -> bool:
        """指定された話者の meta と未導入スタイルをすべてダウンロード・配置 (同期)"""
        official = self.get_official_speakers()
        sp = next((s for s in official if s["prefix"] == prefix), None)
        if not sp:
            logger.error(f"Speaker with prefix '{prefix}' not found.")
            return False

        ver = sp["version"]
        target_dir_name = f"{prefix}-{ver}"
        sp_dir = self.model_dir / target_dir_name
        sp_dir.mkdir(parents=True, exist_ok=True)
        model_base_dir = sp_dir / "model"
        model_base_dir.mkdir(parents=True, exist_ok=True)
        base_resolved = sp_dir.resolve()
        model_base_resolved = model_base_dir.resolve()

        # 1. meta zip のダウンロード・展開（metas.json が無ければ）
        if not (sp_dir / "metas.json").exists():
            meta_url = sp["metaDownloadUrl"]
            logger.info(f"Downloading meta for {sp['speakerName']} from {meta_url}")

            def _extract_meta(z: zipfile.ZipFile):
                for member in z.infolist():
                    parts = Path(member.filename).parts
                    if len(parts) > 1:
                        rel_path = Path(*parts[1:])
                        dest_path = (sp_dir / rel_path).resolve()
                        # Zip Slip (CWE-22) 防御ガード: 展開先が sp_dir の配下に収まることを厳密検証
                        if not dest_path.is_relative_to(base_resolved):
                            logger.warning(f"⚠️ [Security] Skipping malicious zip entry traversing path: {member.filename}")
                            continue
                        if member.is_dir():
                            dest_path.mkdir(parents=True, exist_ok=True)
                        else:
                            dest_path.parent.mkdir(parents=True, exist_ok=True)
                            with open(dest_path, "wb") as f_out:
                                f_out.write(z.read(member.filename))

            try:
                self._download_and_extract_zip(meta_url, _extract_meta)
            except Exception as e:
                logger.error(f"Failed to download/extract meta for {prefix}: {e}")
                return False

        # 2. 未導入スタイルのダウンロード・展開
        for st in sp.get("styles", []):
            style_id = st["styleId"]
            style_dir = model_base_dir / str(style_id)
            has_model = False
            if style_dir.exists():
                has_model = any(f.name.endswith(".pth") for f in style_dir.iterdir() if f.is_file())

            if not has_model:
                style_url = st["downloadUrl"]
                logger.info(f"Downloading style {style_id} ({st['styleName']}) for {sp['speakerName']}...")

                def _extract_style(z: zipfile.ZipFile):
                    for member in z.infolist():
                        parts = Path(member.filename).parts
                        if len(parts) >= 1:
                            if parts[0] == str(style_id):
                                rel_path = Path(*parts)
                            else:
                                rel_path = Path(str(style_id)) / Path(*parts)
                            dest_path = (model_base_dir / rel_path).resolve()
                            # Zip Slip (CWE-22) 防御ガード: 展開先が model_base_dir の配下に収まることを厳密検証
                            if not dest_path.is_relative_to(model_base_resolved):
                                logger.warning(f"⚠️ [Security] Skipping malicious zip entry traversing path: {member.filename}")
                                continue
                            if member.is_dir():
                                dest_path.mkdir(parents=True, exist_ok=True)
                            else:
                                dest_path.parent.mkdir(parents=True, exist_ok=True)
                                with open(dest_path, "wb") as f_out:
                                    f_out.write(z.read(member.filename))

                try:
                    self._download_and_extract_zip(style_url, _extract_style)
                except Exception as e:
                    logger.error(f"Failed to download/extract style {style_id} for {prefix}: {e}")
                    continue

        return True

    async def download_speaker(self, prefix: str) -> bool:
        return await asyncio.to_thread(self.download_speaker_sync, prefix)

    async def download_all(self, on_finish: Optional[Callable[[], Any]] = None):
        """未導入の全モデルをバックグラウンドで一括ダウンロード"""
        if self.is_downloading:
            logger.warning("COEIROINK model download is already running.")
            return

        async with self._lock:
            self.is_downloading = True
            self.progress["is_running"] = True
            self.progress["error"] = None

            try:
                status = self.get_status()
                official = self.get_official_speakers()

                uninstalled_tasks = []
                for sp in status["speakers"]:
                    for st in sp["styles"]:
                        if not st["is_installed"]:
                            uninstalled_tasks.append((sp["prefix"], sp["speakerName"], st["styleId"], st["styleName"]))

                self.progress["total"] = len(uninstalled_tasks)
                self.progress["completed"] = 0
                self.progress["percent"] = 0

                logger.info(f"Starting batch download of {len(uninstalled_tasks)} uninstalled COEIROINK styles...")

                prefixes_to_download = sorted(list(set(t[0] for t in uninstalled_tasks)))

                for prefix in prefixes_to_download:
                    sp_item = next((s for s in official if s["prefix"] == prefix), None)
                    sp_name = sp_item["speakerName"] if sp_item else prefix
                    self.progress["current_speaker"] = sp_name
                    self.progress["message"] = f"キャラクター「{sp_name}」のモデルをダウンロード中..."

                    await self.download_speaker(prefix)

                    current_status = self.get_status()
                    processed_styles = current_status["summary"]["installed_styles"]
                    total_styles = current_status["summary"]["total_styles"]
                    self.progress["completed"] = processed_styles
                    self.progress["percent"] = int((processed_styles / total_styles) * 100) if total_styles > 0 else 100

                self.progress["message"] = "全モデルのダウンロードと配置が完了しました！"
                self.progress["percent"] = 100
                logger.info("All COEIROINK models downloaded successfully.")

                if on_finish:
                    if asyncio.iscoroutinefunction(on_finish):
                        await on_finish()
                    else:
                        on_finish()

            except Exception as e:
                logger.error(f"Error during COEIROINK model batch download: {e}", exc_info=True)
                self.progress["error"] = str(e)
                self.progress["message"] = f"エラーが発生しました: {e}"
            finally:
                self.is_downloading = False
                self.progress["is_running"] = False
