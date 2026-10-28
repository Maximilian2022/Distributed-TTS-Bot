import time
import logging
import re
from typing import Dict, Any, Optional, Tuple, List

logger = logging.getLogger(__name__)

# 各エンジン別・ハードウェア別の初期推論特性基準値 (事前分布 / コールドスタート用)
# 実績ログ蓄積後は EWMA (指数移動平均) により各実機・各エンジンの真値に自動収束
ENGINE_INITIAL_PRIORS = {
    "COEIROINK": {
        "gpu_top": {"base_ms": 100.0, "ms_per_char": 20.0},
        "gpu_mid": {"base_ms": 150.0, "ms_per_char": 45.0},
        "gpu_gen": {"base_ms": 120.0, "ms_per_char": 30.0},
        "cpu":     {"base_ms": 320.0, "ms_per_char": 130.0},  # COEIROINK CPU推論は重い
    },
    "VOICEVOX": {
        "gpu_top": {"base_ms": 80.0,  "ms_per_char": 12.0},
        "gpu_mid": {"base_ms": 120.0, "ms_per_char": 30.0},
        "gpu_gen": {"base_ms": 100.0, "ms_per_char": 20.0},
        "cpu":     {"base_ms": 220.0, "ms_per_char": 65.0},   # VOICEVOX CPU推論は中速
    },
    "SHAREVOX": {
        "gpu_top": {"base_ms": 90.0,  "ms_per_char": 15.0},
        "gpu_mid": {"base_ms": 130.0, "ms_per_char": 35.0},
        "gpu_gen": {"base_ms": 110.0, "ms_per_char": 25.0},
        "cpu":     {"base_ms": 250.0, "ms_per_char": 75.0},
    },
}

class NodeLatencyProfile:
    """単一ノード×各エンジンの動的レイテンシプロファイル (同一ノードでもエンジン別に独立管理)"""

    def __init__(
        self,
        node_id: str,
        engine_name: str,
        has_gpu: bool = False,
        gpu_name: str = "",
        vram_mb: int = 0,
        machine_name: str = "",
        ip: str = ""
    ):
        self.node_id = node_id
        self.engine_name = engine_name.upper()
        self.has_gpu = has_gpu
        self.gpu_name = gpu_name
        self.vram_mb = vram_mb
        self.machine_name = machine_name
        self.ip = ip

        # エンジン別・ハードウェア別のコールドスタート初期値 (VRAM容量ベース + 汎用識別子フォールバック)
        eng_priors = ENGINE_INITIAL_PRIORS.get(self.engine_name, ENGINE_INITIAL_PRIORS["VOICEVOX"])
        if has_gpu:
            g_upper = (gpu_name or "").upper()
            if vram_mb >= 16000 or any(k in g_upper for k in ("4090", "4080", "3090", "3080", "7900", "A100", "H100")):
                p = eng_priors["gpu_top"]
            elif vram_mb >= 8000 or any(k in g_upper for k in ("4070", "3070", "3060", "2080", "1080", "1070", "6800", "6700", "7800", "7700")):
                p = eng_priors["gpu_mid"]
            else:
                p = eng_priors["gpu_gen"]
            self.base_latency_ms = p["base_ms"]
            self.ms_per_char = p["ms_per_char"]
        else:
            p = eng_priors["cpu"]
            self.base_latency_ms = p["base_ms"]
            self.ms_per_char = p["ms_per_char"]

        self.sample_count = 0
        self.last_updated = time.time()
        self.last_synth_ms = 0.0

    def record(self, text_len: int, synth_ms: float) -> None:
        """推論実績を反映してプロファイルを更新 (EWMA: 指数移動平均)"""
        if text_len <= 0 or synth_ms <= 0:
            return

        self.sample_count += 1
        self.last_synth_ms = synth_ms
        self.last_updated = time.time()

        alpha = 0.25  # 直近の実績を重視

        # 1文字あたりの時間 (最小でも 3ms/字)
        instant_rate = max(3.0, (synth_ms - max(20.0, self.base_latency_ms * 0.5)) / max(1, text_len))

        self.ms_per_char = round(alpha * instant_rate + (1.0 - alpha) * self.ms_per_char, 1)

        # 基本オーバーヘッドの微調整 (短文の場合に強く影響)
        if text_len <= 5:
            self.base_latency_ms = round(alpha * max(20.0, synth_ms - text_len * self.ms_per_char) + (1.0 - alpha) * self.base_latency_ms, 1)

    def estimate(self, text_len: int) -> float:
        """指定文字数の予測合成時間をミリ秒で算出"""
        return max(30.0, self.base_latency_ms + (text_len * self.ms_per_char))

    def calc_max_chars(
        self,
        target_budget_ms: float = 1750.0,
        min_chars: int = 8,
        max_chars: int = 30
    ) -> int:
        """
        目標許容時間 (target_budget_ms) 以内に推論完了する最大文字数を逆算。
        - 実測値 (base_latency_ms, ms_per_char) に基づき自律適応。
        - min_chars 〜 max_chars の範囲に安全クランプ。
        """
        safe_rate = max(1.0, self.ms_per_char)
        available_time = max(0.0, target_budget_ms - self.base_latency_ms)
        chars = int(available_time / safe_rate)
        return max(min_chars, min(chars, max_chars))

    @property
    def gpu_power_score(self) -> float:
        """
        GPU性能スコア (数値が大きいほど高性能)
        ハードウェアスペック (VRAM容量 / アーキテクチャ世代) + EWMA実測推論速度を統合して環境非依存に格付け。
        """
        if not self.has_gpu:
            return 0.0

        score = float(self.vram_mb) if self.vram_mb > 0 else 8192.0

        # GPUアーキテクチャ・クラスボーナス (NVIDIA / AMD / Intel 汎用)
        g_upper = (self.gpu_name or "").upper()
        if any(k in g_upper for k in ("5090", "5080", "4090", "4080", "7900", "A100", "H100")):
            score += 35000.0
        elif any(k in g_upper for k in ("4070", "3090", "3080", "6900", "6800", "7800")):
            score += 25000.0
        elif any(k in g_upper for k in ("4060", "3070", "3060", "2080", "TITAN", "6700", "7700", "A770")):
            score += 15000.0
        elif any(k in g_upper for k in ("2070", "2060", "1080", "1070", "6600", "7600", "A750")):
            score += 8000.0
        elif any(k in g_upper for k in ("1660", "1650", "1060")):
            score += 4000.0

        # 実測速度ボーナス (速いほど加点: サンプル蓄積時は実測性能を強く反映)
        if self.ms_per_char > 0:
            weight = 8000.0 if self.sample_count >= 3 else 5000.0
            speed_bonus = (50.0 / max(3.0, self.ms_per_char)) * weight
            score += speed_bonus

        return score

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "engine": self.engine_name,
            "has_gpu": self.has_gpu,
            "gpu_name": self.gpu_name,
            "machine_name": self.machine_name,
            "ip": self.ip,
            "base_latency_ms": round(self.base_latency_ms, 1),
            "ms_per_char": round(self.ms_per_char, 1),
            "sample_count": self.sample_count,
            "last_synth_ms": round(self.last_synth_ms, 1),
            "gpu_power_score": round(self.gpu_power_score, 1),
        }


class EngineLatencyProfiler:
    """各ノードのリアルタイム生成速度を追跡・統括するプロファイラシングルトン"""

    def __init__(self):
        self._profiles: Dict[Tuple[str, str], NodeLatencyProfile] = {}

    def get_or_create(self, node: Any) -> NodeLatencyProfile:
        """ノードオブジェクトからプロファイルを取得または新規作成"""
        node_id = getattr(node, "id", str(node))
        engine_name = getattr(node, "engine_name", "UNKNOWN").upper()
        key = (node_id, engine_name)

        specs = getattr(node, "specs", {}) or {}
        has_gpu = bool(getattr(node, "has_gpu", False) or specs.get("gpu", False))
        gpu_name = str(specs.get("gpu_name", ""))
        vram_mb = int(specs.get("gpu_vram_mb", 0) or 0)
        machine_name = getattr(node, "machine_name", "") or ""
        url = getattr(node, "url", "")
        ip = ""
        if url:
            try:
                from urllib.parse import urlparse
                ip = urlparse(url).hostname or ""
            except Exception:
                pass

        if key not in self._profiles:
            self._profiles[key] = NodeLatencyProfile(
                node_id=node_id,
                engine_name=engine_name,
                has_gpu=has_gpu,
                gpu_name=gpu_name,
                vram_mb=vram_mb,
                machine_name=machine_name,
                ip=ip
            )
        else:
            # 既存プロファイルのスペック更新
            p = self._profiles[key]
            if has_gpu and not p.has_gpu:
                p.has_gpu = has_gpu
            if gpu_name and not p.gpu_name:
                p.gpu_name = gpu_name
            if vram_mb and not p.vram_mb:
                p.vram_mb = vram_mb
            if machine_name and not p.machine_name:
                p.machine_name = machine_name
            if ip and not p.ip:
                p.ip = ip

        return self._profiles[key]

    def record_synth(self, node: Any, text_len: int, synth_ms: float) -> None:
        """推論実績を記録"""
        profile = self.get_or_create(node)
        profile.record(text_len, synth_ms)
        logger.debug(
            f"📊 [Profiler] Updated {profile.node_id}/{profile.engine_name}: "
            f"{text_len} chars in {synth_ms:.0f}ms -> rate: {profile.ms_per_char:.1f}ms/char (base: {profile.base_latency_ms:.0f}ms)"
        )

    def estimate_ms(self, node: Any, text_len: int) -> float:
        """指定ノードでの完了予測時間を取得"""
        profile = self.get_or_create(node)
        return profile.estimate(text_len)

    def rank_nodes_by_power(self, nodes: List[Any]) -> List[Any]:
        """
        ノード一覧をGPU性能スコアの降順でソート (最強GPUが先頭)
        同点の場合はアクティブ実行数の少なさ・優先度で整列。
        """
        def _score_key(n: Any):
            prof = self.get_or_create(n)
            power = prof.gpu_power_score
            active = getattr(n, "active_count", 0)
            prio = getattr(n, "priority", 1)
            # power降順 (-power), active昇順 (active), prio昇順 (prio)
            return (-power, active, prio)

        return sorted(nodes, key=_score_key)

    def get_strongest_gpu_node(self, nodes: List[Any]) -> Optional[Any]:
        """健全で利用可能なGPUノードの中で最も強力なノードを取得"""
        available_gpus = [n for n in nodes if getattr(n, "is_available", True) and getattr(n, "has_gpu", False)]
        if not available_gpus:
            return None
        ranked = self.rank_nodes_by_power(available_gpus)
        return ranked[0] if ranked else None

    def get_dynamic_max_chars(
        self,
        engine: Optional[str] = None,
        target_budget_ms: float = 1750.0,
        min_chars: int = 8,
        max_chars: int = 30,
        candidate_nodes: Optional[List[Any]] = None
    ) -> int:
        """
        指定エンジンの現在の実測推論性能に基づき、目標許容時間 (target_budget_ms) 以内に
        安全に合成完了できる動的な最大文字数を算出する。
        - クラウドTTS (azure, google, edge 等) や軽量ルール (openjtalk): 固定の max_chars (30) を返却。
        - ローカルTTS (coeiroink, voicevox, sharevox 等):
          1. candidate_nodes (候補ノード) が指定されている場合: 最有力ノードのプロファイルを採用。
          2. 指定がない場合: self._profiles から該当エンジンの有効プロファイルを検索。
             - GPUノードが存在する場合は最強GPUノードのプロファイルを採用。
             - CPUノードのみの場合は、最も代表的なCPUノードのプロファイルを採用。
          3. プロファイルが未登録の場合:
             - クラスタ全体のGPU有無に応じた安全な初期値（GPU: max_chars, CPU: 13文字）を返却。
        """
        if not engine:
            return max_chars

        eng_lower = engine.strip().lower()
        LOCAL_ENGINES = {"coeiroink", "voicevox", "sharevox"}
        if eng_lower not in LOCAL_ENGINES:
            return max_chars

        eng_upper = eng_lower.upper()

        # 該当エンジンのプロファイルを収集
        matched_profiles: List[NodeLatencyProfile] = []
        if candidate_nodes:
            for n in candidate_nodes:
                p = self.get_or_create(n)
                if p.engine_name == eng_upper:
                    matched_profiles.append(p)
        else:
            for (nid, e_name), p in self._profiles.items():
                if e_name == eng_upper:
                    matched_profiles.append(p)

        if not matched_profiles:
            # 該当エンジンの登録がまだない場合の初期推論基準値からエンジン別に逆算
            eng_prior = ENGINE_INITIAL_PRIORS.get(eng_upper, ENGINE_INITIAL_PRIORS["VOICEVOX"])
            has_any_gpu = any(p.has_gpu for p in self._profiles.values())
            prior = eng_prior["gpu_gen"] if has_any_gpu else eng_prior["cpu"]
            avail_ms = max(0.0, target_budget_ms - prior["base_ms"])
            chars = int(avail_ms / max(1.0, prior["ms_per_char"]))
            return max(min_chars, min(chars, max_chars))

        # GPUノードがあれば最強GPUノードのプロファイルを採用
        gpu_profiles = [p for p in matched_profiles if p.has_gpu]
        if gpu_profiles:
            best_gpu = max(gpu_profiles, key=lambda p: p.gpu_power_score)
            return best_gpu.calc_max_chars(target_budget_ms, min_chars, max_chars)

        # CPUノードのみの場合: サンプル数が多く安定したプロファイルを採用
        cpu_profiles = matched_profiles
        if cpu_profiles:
            best_cpu = max(cpu_profiles, key=lambda p: (p.sample_count, -p.ms_per_char))
            return best_cpu.calc_max_chars(target_budget_ms, min_chars, max_chars)

        return max_chars

    def get_all_stats(self) -> Dict[str, Any]:
        """全プロファイルの統計データを取得 (WebUI API用)"""
        node_dicts = {}
        strongest_map: Dict[str, Tuple[str, float]] = {}
        strongest_machines: Dict[str, str] = {}

        # 1. 各エンジンごとの最高スコアGPUを算出
        for (node_id, engine_name), p in self._profiles.items():
            if p.has_gpu:
                curr = strongest_map.get(engine_name)
                if not curr or p.gpu_power_score > curr[1]:
                    strongest_map[engine_name] = (node_id, p.gpu_power_score)
                    strongest_machines[engine_name] = p.machine_name

        # 2. 各ノード辞書の生成 (is_strongest_gpu フラグを付与)
        for (node_id, engine_name), p in self._profiles.items():
            k = f"{node_id}:{engine_name}"
            d = p.to_dict()
            is_strongest = (
                p.has_gpu and
                engine_name in strongest_map and
                strongest_map[engine_name][0] == node_id
            )
            d["is_strongest_gpu"] = is_strongest
            node_dicts[k] = d

        return {
            "nodes": node_dicts,
            "strongest_nodes": {eng.lower(): nid for eng, (nid, _) in strongest_map.items()},
            "strongest_machines": {eng.lower(): m for eng, m in strongest_machines.items() if m},
            "list": list(node_dicts.values())
        }

# グローバルシングルトンインスタンス
latency_profiler = EngineLatencyProfiler()
