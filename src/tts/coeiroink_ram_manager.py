import os
import time
import shutil
import asyncio
import logging
from typing import Optional, Dict, Set, Tuple, List, Any, Set
from pathlib import Path

logger = logging.getLogger(__name__)


def get_host_memory_usage() -> Tuple[float, float, float]:
    """
    ホストのメモリ使用率を /proc/meminfo からゼロオーバーヘッドで取得
    Returns: (usage_percent, used_gb, total_gb)
    """
    try:
        with open("/proc/meminfo", "r") as f:
            mem = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    mem[parts[0].strip()] = int(parts[1].strip().split()[0])
        total_kb = mem.get("MemTotal", 0)
        avail_kb = mem.get("MemAvailable", 0)
        if total_kb <= 0:
            return 0.0, 0.0, 0.0
        used_kb = total_kb - avail_kb
        percent = (used_kb / total_kb) * 100.0
        return percent, used_kb / (1024 * 1024), total_kb / (1024 * 1024)
    except Exception as e:
        logger.warning(f"[CoeiroinkRamManager] /proc/meminfo 取得失敗: {e}")
        return 0.0, 0.0, 0.0


def get_dynamic_pin_limit(total_ram_gb: Optional[float] = None) -> int:
    """
    ホスト総RAM容量 (GB) に応じて安全にRAMDiskへピン留め常駐できる最大モデル数を動的算出。
    - <= 8.5GB : 1モデル
    - <= 16.5GB: 2モデル
    - <= 32.5GB: 4モデル
    - >  32.5GB: 6モデル
    """
    if total_ram_gb is None or total_ram_gb <= 0:
        _, _, total_ram_gb = get_host_memory_usage()
    if total_ram_gb <= 0:
        return 2
    if total_ram_gb <= 8.5:
        return 1
    if total_ram_gb <= 16.5:
        return 2
    if total_ram_gb <= 32.5:
        return 4
    return 6


def get_dir_size_bytes(path: str) -> int:
    """指定ディレクトリの合計サイズ (bytes) を再帰的に計測"""
    total = 0
    if not path or not os.path.exists(path):
        return 0
    try:
        for root, _dirs, files in os.walk(path):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    if not os.path.islink(fpath):
                        total += os.path.getsize(fpath)
                except OSError:
                    pass
    except Exception:
        pass
    return total


class CoeiroinkRamManager:
    """
    COEIROINK 動的RAM Disk (tmpfs) マネージャー
    - 相対シンボリックリンク二層構造による安全・高速なモデルキャッシュ管理
    - ホストメモリ二段階セーフティネット (警戒: 85% / 緊急: 90% / 復帰: 70%)
    - アトミック昇格 (.tmp経由) / 二段階遅延解放 (Race Condition完全防止)
    - 起動時オートヒーリング (Broken Symlinkの自動SSD初期化)
    """

    WARNING_THRESHOLD_PERCENT = 85.0   # 警戒水域: 非ピン留めモデルのLRU解放
    CRITICAL_THRESHOLD_PERCENT = 90.0  # 緊急水域: 全モデル緊急解放 & SSD退避
    RECOVERY_THRESHOLD_PERCENT = 70.0  # 復帰水域: クールダウン解除ライン
    FREEZE_DURATION_SECONDS = 300.0    # 緊急解放後の昇格凍結時間 (5分)

    def __init__(
        self,
        ssd_dir: str = "/app/coeiroink_models",
        ram_dir: str = "/app/coeiroink_ram",
        hybrid_dir: str = "/app/coeiroink_hybrid",
        alert_service: Optional[Any] = None,
        db: Optional[Any] = None,
        config: Optional[Any] = None
    ):
        self.ssd_dir = ssd_dir
        self.ram_dir = ram_dir
        self.hybrid_dir = hybrid_dir
        self.alert_service = alert_service
        self.db = db
        self.config = config

        self._uuid_to_model_dir: Dict[str, str] = {}
        self._pinned_models: Set[str] = set()
        self._active_ram_models: Dict[str, float] = {}  # model_dir -> last_accessed_time
        self._lock = asyncio.Lock()
        self._freeze_until: float = 0.0
        self._is_running = False
        self._watchdog_task: Optional[asyncio.Task] = None

    async def initialize(self) -> None:
        """初期化と起動時オートヒーリング"""
        os.makedirs(self.ssd_dir, exist_ok=True)
        os.makedirs(self.ram_dir, exist_ok=True)
        os.makedirs(self.hybrid_dir, exist_ok=True)

        # 1. SSDモデルのUUIDスキャン
        await self._scan_ssd_models()

        # 2. 起動時オートヒーリング（全シンボリックリンクをSSD実体へ安全初期化）
        await self._auto_heal_symlinks()

        # 3. 個人設定（ピン留めモデル）の同期と昇格
        if self.db:
            await self.sync_pinned_models()

        # 4. メモリ監視ウォッチドッグの開始
        self._is_running = True
        self._watchdog_task = asyncio.create_task(self.memory_watchdog_loop())
        logger.info(
            f"🚀 [CoeiroinkRamManager] 初期化完了: モデル数={len(self._uuid_to_model_dir)}, "
            f"ピン留め={list(self._pinned_models)}, RAM常駐={list(self._active_ram_models.keys())}"
        )

    async def close(self) -> None:
        """終了処理"""
        self._is_running = False
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass

    async def _scan_ssd_models(self) -> None:
        """SSD上の全モデルフォルダから metas.json を読み取り UUID マッピングを構築"""
        def _scan():
            mapping = {}
            if not os.path.exists(self.ssd_dir):
                return mapping
            for entry in os.scandir(self.ssd_dir):
                if entry.is_dir() and not entry.name.startswith("."):
                    metas_path = os.path.join(entry.path, "metas.json")
                    if os.path.exists(metas_path):
                        try:
                            import json
                            with open(metas_path, "r", encoding="utf-8") as f:
                                data = json.load(f)
                                uuid_val = data.get("speakerUuid")
                                if uuid_val:
                                    mapping[uuid_val] = entry.name
                        except Exception as e:
                            logger.warning(f"[CoeiroinkRamManager] metas.json 読み込み失敗 ({entry.name}): {e}")
            return mapping

        self._uuid_to_model_dir = await asyncio.to_thread(_scan)
        logger.info(f"🔍 [CoeiroinkRamManager] SSDモデルスキャン完了: {len(self._uuid_to_model_dir)} キャラクター検出")

    def resolve_model_dir(self, uuid_or_dir: str) -> Optional[str]:
        """UUIDまたはフォルダ名から正式なモデルフォルダ名を取得"""
        if uuid_or_dir in self._uuid_to_model_dir:
            return self._uuid_to_model_dir[uuid_or_dir]
        if uuid_or_dir in self._uuid_to_model_dir.values():
            return uuid_or_dir
        # 前方一致 (例: lilin ➔ lilin-2.1.0)
        for u, d in self._uuid_to_model_dir.items():
            if d.startswith(uuid_or_dir) or uuid_or_dir.startswith(u):
                return d
        return None

    async def _auto_heal_symlinks(self) -> None:
        """
        起動時オートヒーリング:
        - RAMDisk上の不完全な .tmp ディレクトリを消去
        - hybrid ディレクトリ内の全シンボリックリンクを検査し、
          壊れたリンクや初期状態のリンクをすべて ../storage/ssd/<model> に安全接続
        """
        def _heal():
            # 1. RAM上の .tmp 残骸消去
            if os.path.exists(self.ram_dir):
                for entry in os.scandir(self.ram_dir):
                    if entry.name.endswith(".tmp"):
                        try:
                            shutil.rmtree(entry.path, ignore_errors=True)
                            logger.info(f"[CoeiroinkRamManager] 起動時クリーンアップ: {entry.name} を削除")
                        except Exception:
                            pass

            # 2. 全モデルについて hybrid 内のシンボリックリンクを初期化
            for model_dir in self._uuid_to_model_dir.values():
                link_path = os.path.join(self.hybrid_dir, model_dir)
                target_rel_path = f"../storage/ssd/{model_dir}"

                # 現在のリンク先が RAM に存在するか確認
                ram_model_path = os.path.join(self.ram_dir, model_dir)
                if os.path.islink(link_path):
                    current_target = os.readlink(link_path)
                    if "../storage/ram/" in current_target and os.path.exists(ram_model_path):
                        # 既にRAMに存在し健全な場合は維持
                        continue

                # 安全な初期リンク作成 (アトミック replace)
                tmp_link = os.path.join(self.hybrid_dir, f".{model_dir}.tmp_link")
                try:
                    if os.path.exists(tmp_link) or os.path.islink(tmp_link):
                        os.unlink(tmp_link)
                    os.symlink(target_rel_path, tmp_link)
                    os.replace(tmp_link, link_path)
                except Exception as e:
                    logger.warning(f"[CoeiroinkRamManager] シンボリックリンク初期化失敗 ({model_dir}): {e}")

        await asyncio.to_thread(_heal)
        logger.info("🛡️ [CoeiroinkRamManager] 起動時オートヒーリング完了 (全モデルをSSD参照に安全接続)")

    async def promote_model(self, uuid_or_dir: str) -> bool:
        """
        指定モデルを SSD から RAMDisk へ昇格 (アトミック配置 & リンク差し替え)
        """
        model_dir = self.resolve_model_dir(uuid_or_dir)
        if not model_dir:
            logger.debug(f"[CoeiroinkRamManager] 昇格対象モデルが見つかりません: {uuid_or_dir}")
            return False

        now = time.time()

        # クールダウン凍結中チェック
        if self._freeze_until > now:
            remaining = int(self._freeze_until - now)
            logger.debug(f"[CoeiroinkRamManager] 緊急解放クールダウン中のため昇格スキップ ({model_dir}, 残り {remaining}秒)")
            return False

        # メモリ使用率チェック (警戒ライン 85% 以上なら昇格見送り)
        mem_percent, _, _ = get_host_memory_usage()
        if mem_percent >= self.WARNING_THRESHOLD_PERCENT:
            logger.warning(
                f"⚠️ [CoeiroinkRamManager] ホストメモリ警戒域 ({mem_percent:.1f}% >= {self.WARNING_THRESHOLD_PERCENT}%) "
                f"のため昇格をブロックしました: {model_dir}"
            )
            return False

        async with self._lock:
            ram_path = os.path.join(self.ram_dir, model_dir)
            link_path = os.path.join(self.hybrid_dir, model_dir)

            # 既にRAM上に存在する場合はタイムスタンプ更新のみ
            if os.path.exists(ram_path) and os.path.islink(link_path):
                cur_link = os.readlink(link_path)
                if "../storage/ram/" in cur_link:
                    self._active_ram_models[model_dir] = now
                    return True

            ssd_path = os.path.join(self.ssd_dir, model_dir)
            if not os.path.exists(ssd_path):
                logger.error(f"[CoeiroinkRamManager] SSD上に実体モデルが存在しません: {ssd_path}")
                return False

            logger.info(f"🚀 [CoeiroinkRamManager] モデル昇格開始 (SSD ➔ RAMDisk): {model_dir}")
            t0 = time.time()

            # アトミック配置: .tmp に完全コピー後に rename
            def _copy_and_link():
                tmp_dir = os.path.join(self.ram_dir, f"{model_dir}.tmp")
                if os.path.exists(tmp_dir):
                    shutil.rmtree(tmp_dir, ignore_errors=True)

                # 高速な Linux ネイティブ cp -r (copy_file_range 最適化) を実行
                import subprocess
                ret = subprocess.run(["cp", "-r", ssd_path, tmp_dir], check=False)
                if ret.returncode != 0:
                    raise RuntimeError(f"cp -r failed with code {ret.returncode}")

                if os.path.exists(ram_path):
                    shutil.rmtree(ram_path, ignore_errors=True)
                os.replace(tmp_dir, ram_path)

                # アトミックなシンボリックリンク差し替え
                tmp_link = os.path.join(self.hybrid_dir, f".{model_dir}.tmp_link")
                if os.path.exists(tmp_link) or os.path.islink(tmp_link):
                    os.unlink(tmp_link)
                os.symlink(f"../storage/ram/{model_dir}", tmp_link)
                os.replace(tmp_link, link_path)

            try:
                await asyncio.to_thread(_copy_and_link)
                elapsed = time.time() - t0
                self._active_ram_models[model_dir] = time.time()
                logger.info(f"✨ [CoeiroinkRamManager] モデル昇格完了: {model_dir} (所要時間: {elapsed:.2f}s)")
                return True
            except Exception as e:
                logger.error(f"❌ [CoeiroinkRamManager] モデル昇格失敗 ({model_dir}): {e}", exc_info=True)
                # 万一の失敗時は安全にSSDリンクへ戻す
                self._switch_link_to_ssd_sync(model_dir)
                return False

    def _switch_link_to_ssd_sync(self, model_dir: str) -> None:
        """シンボリックリンクをSSDへ同期的に即時切り替え"""
        link_path = os.path.join(self.hybrid_dir, model_dir)
        tmp_link = os.path.join(self.hybrid_dir, f".{model_dir}.tmp_link")
        try:
            if os.path.exists(tmp_link) or os.path.islink(tmp_link):
                os.unlink(tmp_link)
            os.symlink(f"../storage/ssd/{model_dir}", tmp_link)
            os.replace(tmp_link, link_path)
        except Exception as e:
            logger.warning(f"[CoeiroinkRamManager] SSDリンク差し戻し失敗 ({model_dir}): {e}")

    async def demote_model(self, model_dir: str, force: bool = False, delay_seconds: float = 3.0) -> bool:
        """
        指定モデルを RAMDisk から解放し SSD 参照へ戻す (二段階遅延解放)
        - force=False の場合、ピン留めモデルは解放を保護
        - delay_seconds: 進行中の推論fd完了待ちディレイ
        """
        if not force and model_dir in self._pinned_models:
            logger.debug(f"[CoeiroinkRamManager] ピン留めモデルのため解放スキップ: {model_dir}")
            return False

        async with self._lock:
            ram_path = os.path.join(self.ram_dir, model_dir)
            if not os.path.exists(ram_path):
                self._active_ram_models.pop(model_dir, None)
                self._switch_link_to_ssd_sync(model_dir)
                return True

            logger.info(f"🧹 [CoeiroinkRamManager] モデル解放開始 (二段階解放): {model_dir}")
            # ステップ1: リンクをSSD参照へ即時差し戻し (新規リクエストを安全にSSDへ流す)
            self._switch_link_to_ssd_sync(model_dir)
            self._active_ram_models.pop(model_dir, None)

        # ステップ2: 進行中リクエストの安全完了を待ってからRAM上の実体ファイルを削除
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        def _cleanup():
            if os.path.exists(ram_path):
                shutil.rmtree(ram_path, ignore_errors=True)

        await asyncio.to_thread(_cleanup)
        logger.info(f"✨ [CoeiroinkRamManager] モデル解放完了 (RAMDisk空き容量返還): {model_dir}")
        return True

    async def emergency_flush(self) -> None:
        """
        【緊急セーフティネット (>= 90%)】
        全モデルを一斉にSSD参照へ差し戻し、RAMDiskを全解放してOSへ即座にメモリを返還する
        """
        mem_percent, used_gb, total_gb = get_host_memory_usage()
        logger.critical(
            f"🚨 [CoeiroinkRamManager] 【緊急メモリ全解放発動】ホストメモリ使用率: {mem_percent:.1f}% "
            f"({used_gb:.1f}GB / {total_gb:.1f}GB) >= {self.CRITICAL_THRESHOLD_PERCENT}%！"
        )

        async with self._lock:
            # 1. 全リンクをSSD実体へ一瞬で差し戻し
            for model_dir in self._uuid_to_model_dir.values():
                self._switch_link_to_ssd_sync(model_dir)
            self._active_ram_models.clear()

            # 2. クールダウン凍結設定 (5分間)
            self._freeze_until = time.time() + self.FREEZE_DURATION_SECONDS

        # 3. RAM実体ディレクトリの全ファイルを遅延削除
        await asyncio.sleep(2.0)

        def _flush_ram():
            if os.path.exists(self.ram_dir):
                for entry in os.scandir(self.ram_dir):
                    try:
                        if entry.is_dir():
                            shutil.rmtree(entry.path, ignore_errors=True)
                        else:
                            os.unlink(entry.path)
                    except Exception:
                        pass

        await asyncio.to_thread(_flush_ram)
        logger.info("🛡️ [CoeiroinkRamManager] 緊急全解放完了。OSへRAMDiskメモリを全返還しました。")

        # 4. 管理者へ Discord DM で緊急アラート送信
        if self.alert_service:
            try:
                await self.alert_service.send_alert(
                    title="🚨 ホストメモリ緊急圧迫検知・RAMDisk全解放",
                    message=(
                        f"ホストメモリ使用率が **{mem_percent:.1f}%** ({used_gb:.1f}GB / {total_gb:.1f}GB) に達したため、"
                        f"OOM防止の安全策として COEIROINK RAMDisk 内の全モデルを即時解放し、SSD参照へ退避しました。\n"
                        f"・読み上げ処理はSSD経由で停止せず継続稼働します。\n"
                        f"・メモリが {self.RECOVERY_THRESHOLD_PERCENT}% 以下に安定するまで5分間昇格を凍結します。"
                    ),
                    category="ENGINE_MASTER",
                    severity="CRITICAL",
                    details=f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                )
            except Exception as e:
                logger.warning(f"[CoeiroinkRamManager] アラート送信失敗: {e}")

    async def sync_pinned_models(self) -> None:
        """DBから個人設定された COEIROINK モデルを取得しピン留め・昇格 (直近30日アクティブ優先・上限4モデル)"""
        if not self.db:
            return
        try:
            if hasattr(self.db, "get_pinned_coeiroink_speaker_ids"):
                speaker_ids = await self.db.get_pinned_coeiroink_speaker_ids(limit=4, active_days=30)
            else:
                async with self.db.get_connection() as conn:
                    async with conn.execute(
                        "SELECT DISTINCT speaker_id FROM ("
                        "  SELECT speaker_id FROM user_settings WHERE speaker_id LIKE 'ci_%'"
                        "  UNION"
                        "  SELECT speaker_id FROM guild_user_settings WHERE speaker_id LIKE 'ci_%'"
                        ")"
                    ) as cursor:
                        rows = await cursor.fetchall()
                speaker_ids = [r[0] for r in rows if r[0]]

            new_pinned: Set[str] = set()
            for spk_id in speaker_ids:
                model_dir = None
                # config または voices から speakerUuid を特定
                cfg = self.config if self.config else getattr(self.db, "config", None)
                if cfg and getattr(cfg, "voices", None):
                    v_info = cfg.voices.get(spk_id)
                    if v_info and getattr(v_info, "voice_name", None):
                        spk_uuid = v_info.voice_name.split("_")[0]
                        model_dir = self.resolve_model_dir(spk_uuid)

                if not model_dir:
                    model_dir = self.resolve_model_dir(spk_id)

                if model_dir:
                    new_pinned.add(model_dir)

            self._pinned_models = new_pinned
            logger.info(f"📌 [CoeiroinkRamManager] 個人設定ピン留めモデル同期: {list(self._pinned_models)}")

            # ピン留めモデルを優先昇格
            for p_model in self._pinned_models:
                await self.promote_model(p_model)

        except Exception as e:
            logger.warning(f"[CoeiroinkRamManager] ピン留めモデル同期エラー: {e}")

    async def memory_watchdog_loop(self) -> None:
        """2秒周期のホストメモリ監視ループ"""
        logger.info("👀 [CoeiroinkRamManager] メモリ監視ウォッチドッグ開始 (2秒周期)")
        while self._is_running:
            try:
                await asyncio.sleep(2.0)
                mem_percent, used_gb, total_gb = get_host_memory_usage()

                # 1. 緊急ライン (>= 90%)
                if mem_percent >= self.CRITICAL_THRESHOLD_PERCENT:
                    await self.emergency_flush()
                    continue

                # 2. 警戒ライン (>= 85%) ➔ 非ピン留めモデルのLRU解放
                if mem_percent >= self.WARNING_THRESHOLD_PERCENT:
                    logger.warning(
                        f"⚠️ [CoeiroinkRamManager] メモリ警戒水域検知: {mem_percent:.1f}% >= {self.WARNING_THRESHOLD_PERCENT}% "
                        f"({used_gb:.1f}GB / {total_gb:.1f}GB) - 非ピン留めモデルを古い順に解放します"
                    )
                    # 最も古いアクセス時刻の非ピン留めモデルを特定して解放
                    candidates = [
                        (m, t) for m, t in self._active_ram_models.items()
                        if m not in self._pinned_models
                    ]
                    if candidates:
                        candidates.sort(key=lambda x: x[1])  # 古い順
                        oldest_model = candidates[0][0]
                        await self.demote_model(oldest_model, force=False)

                # 3. 復帰チェック (<= 70% かつ凍結期間満了)
                now = time.time()
                if self._freeze_until > 0 and now >= self._freeze_until:
                    if mem_percent <= self.RECOVERY_THRESHOLD_PERCENT:
                        logger.info(
                            f"✨ [CoeiroinkRamManager] メモリ安全圏復帰 ({mem_percent:.1f}% <= {self.RECOVERY_THRESHOLD_PERCENT}%): "
                            f"緊急昇格凍結を解除しました。"
                        )
                        self._freeze_until = 0.0
                        # ピン留めモデルの再昇格を試みる
                        for p_model in self._pinned_models:
                            await self.promote_model(p_model)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CoeiroinkRamManager] ウォッチドッグ例外: {e}", exc_info=True)
                await asyncio.sleep(5.0)
