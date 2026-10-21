import os
import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger(__name__)

# モデルマニフェストのインメモリキャッシュ: {model_name: (mtime, manifest_dict)}
_MANIFEST_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}

def calculate_file_sha256(filepath: Path, chunk_size: int = 256 * 1024) -> str:
    """ファイルの SHA-256 ハッシュを計算"""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()

def get_model_dir_mtime(model_dir: Path) -> float:
    """モデルディレクトリ配下の最新更新日時（mtime）を高速取得（0.1ms）"""
    max_mtime = 0.0
    try:
        meta_file = model_dir / "metas.json"
        if meta_file.exists():
            max_mtime = max(max_mtime, meta_file.stat().st_mtime)
    except OSError:
        pass

    try:
        model_sub = model_dir / "model"
        if model_sub.exists():
            max_mtime = max(max_mtime, model_sub.stat().st_mtime)
    except OSError:
        pass

    return max_mtime

def build_model_manifest(model_dir: Path, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
    """
    指定されたモデルディレクトリのマニフェストを生成・返却。
    - 各ファイルの相対パス、サイズ、SHA-256 ハッシュを記録。
    - mtime が変化していない場合はインメモリキャッシュから即座に返却。
    """
    if not model_dir.exists() or not model_dir.is_dir():
        return None

    model_name = model_dir.name
    latest_mtime = get_model_dir_mtime(model_dir)

    if not force_refresh and model_name in _MANIFEST_CACHE:
        cached_mtime, cached_manifest = _MANIFEST_CACHE[model_name]
        if cached_mtime == latest_mtime:
            return cached_manifest

    # ディスクキャッシュ (.manifest.json) の読み込みチェック
    disk_manifest_file = model_dir / ".manifest.json"
    if not force_refresh and disk_manifest_file.exists():
        try:
            with open(disk_manifest_file, "r", encoding="utf-8") as f:
                disk_data = json.load(f)
            if abs(disk_data.get("cached_mtime", 0.0) - latest_mtime) < 0.5:
                cached_manifest = disk_data.get("manifest")
                if cached_manifest and cached_manifest.get("tree_sha256"):
                    _MANIFEST_CACHE[model_name] = (latest_mtime, cached_manifest)
                    return cached_manifest
        except Exception as e:
            logger.debug(f"Failed to read disk manifest cache for {model_name}: {e}")

    # metas.json から基本情報を取得
    meta_path = model_dir / "metas.json"
    speaker_name = model_name
    speaker_uuid = ""
    style_count = 0
    if meta_path.exists():
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            speaker_name = meta.get("speakerName", model_name)
            speaker_uuid = meta.get("speakerUuid", "")
            style_count = len(meta.get("styles", []))
        except Exception:
            pass

    files_list = []
    total_bytes = 0
    tree_hasher = hashlib.sha256()

    all_files = []
    for root, dirs, files in os.walk(model_dir):
        # 一時ディレクトリや隠しファイルをスキップ
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith("."):
                continue
            full_path = Path(root) / f
            rel_path = full_path.relative_to(model_dir).as_posix()
            all_files.append((rel_path, full_path))

    # 決定論的な順序でソート
    all_files.sort(key=lambda x: x[0])

    for rel_path, full_path in all_files:
        try:
            size = full_path.stat().st_size
            sha256 = calculate_file_sha256(full_path)
            files_list.append({
                "path": rel_path,
                "size": size,
                "sha256": sha256
            })
            total_bytes += size
            tree_hasher.update(f"{rel_path}:{sha256}:{size}\n".encode("utf-8"))
        except Exception as e:
            logger.warning(f"Error computing hash for {full_path}: {e}")

    manifest = {
        "model_name": model_name,
        "speaker_name": speaker_name,
        "speaker_uuid": speaker_uuid,
        "style_count": style_count,
        "total_files": len(files_list),
        "total_bytes": total_bytes,
        "size_mb": round(total_bytes / 1024 / 1024, 2),
        "tree_sha256": tree_hasher.hexdigest(),
        "files": files_list
    }

    _MANIFEST_CACHE[model_name] = (latest_mtime, manifest)

    # ディスクキャッシュ (.manifest.json) にアトミック保存
    try:
        disk_manifest_file = model_dir / ".manifest.json"
        tmp_file = model_dir / f".manifest.json.tmp.{os.getpid()}"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump({
                "cached_mtime": latest_mtime,
                "manifest": manifest
            }, f, ensure_ascii=False)
        os.replace(tmp_file, disk_manifest_file)
    except Exception as e:
        logger.warning(f"Failed to persist disk manifest cache for {model_name}: {e}")
        try:
            if tmp_file.exists():
                tmp_file.unlink()
        except Exception:
            pass

    return manifest

def build_all_models_summary(models_root_dir: Path) -> List[Dict[str, Any]]:
    """全モデルのサマリ一覧（各モデルの基本情報 + tree_sha256）を返却"""
    if not models_root_dir.exists() or not models_root_dir.is_dir():
        return []

    summaries = []
    for entry in sorted(models_root_dir.iterdir()):
        if entry.is_dir() and not entry.name.startswith(".") and (entry / "metas.json").exists():
            mf = build_model_manifest(entry)
            if mf:
                summaries.append({
                    "model_name": mf["model_name"],
                    "speaker_name": mf["speaker_name"],
                    "speaker_uuid": mf["speaker_uuid"],
                    "style_count": mf["style_count"],
                    "total_files": mf["total_files"],
                    "total_bytes": mf["total_bytes"],
                    "size_mb": mf["size_mb"],
                    "tree_sha256": mf["tree_sha256"]
                })
    return summaries
