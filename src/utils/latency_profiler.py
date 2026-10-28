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
    "EDGE": {
        # Edge-TTS (WebSocket / ネットワークRTT往復 + クラウド生成)
        "cloud":   {"base_ms": 320.0, "ms_per_char": 12.0},
    },
    "AZURE": {
        # Azure Cognitive Services TTS (高速・低遅延)
        "cloud":   {"base_ms": 240.0, "ms_per_char": 8.0},
    },
    "GOOGLE": {
        # Google Cloud TTS
        "cloud":   {"base_ms": 280.0, "ms_per_char": 10.0},
    },
    "OPENJTALK": {
        # ローカルCバイナリ超軽量推論
        "cpu":     {"base_ms": 50.0,  "ms_per_char": 4.0},
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

        # エンジン別・ハードウェア別のコールドスタート初期値 (クラウド/ローカル/GPU/CPU自動適応)
        eng_priors = ENGINE_INITIAL_PRIORS.get(self.engine_name, ENGINE_INITIAL_PRIORS["VOICEVOX"])
        if "cloud" in eng_priors:
            p = eng_priors["cloud"]
            self.base_latency_ms = p["base_ms"]
            self.ms_per_char = p["ms_per_char"]
        elif has_gpu:
            g_upper = (gpu_name or "").upper()
            if vram_mb >= 16000 or any(k in g_upper for k in ("4090", "4080", "3090", "3080", "7900", "A100", "H100")):
                p = eng_priors.get("gpu_top", eng_priors.get("cpu", {"base_ms": 100.0, "ms_per_char": 20.0}))
            elif vram_mb >= 8000 or any(k in g_upper for k in ("4070", "3070", "3060", "2080", "1080", "1070", "6800", "6700", "7800", "7700")):
                p = eng_priors.get("gpu_mid", eng_priors.get("cpu", {"base_ms": 150.0, "ms_per_char": 35.0}))
            else:
                p = eng_priors.get("gpu_gen", eng_priors.get("cpu", {"base_ms": 120.0, "ms_per_char": 25.0}))
            self.base_latency_ms = p["base_ms"]
            self.ms_per_char = p["ms_per_char"]
        else:
            p = eng_priors.get("cpu", eng_priors.get("cloud", {"base_ms": 220.0, "ms_per_char": 65.0}))
            self.base_latency_ms = p["base_ms"]
            self.ms_per_char = p["ms_per_char"]

        self.sample_count = 0
        self.last_updated = time.time()
        self.last_synth_ms = 0.0

    def record(self, text_len: int, synth_ms: float) -> None:
        """推論実績を反映してプロファイルを更新 (適応型EWMA: 指数移動平均)"""
        if text_len <= 0 or synth_ms <= 0:
            return

        self.sample_count += 1
        self.last_synth_ms = synth_ms
        self.last_updated = time.time()

        # サンプル初期 (N < 4) は実測値への収束を早めるため alpha を動的適応 (1回目で100%、2回目で50%等)
        alpha = max(0.25, 1.0 / float(self.sample_count))

        # 1文字あたりの時間 (最小でも 3ms/字)
        instant_rate = max(3.0, (synth_ms - max(20.0, self.base_latency_ms * 0.5)) / max(1, text_len))

        self.ms_per_char = round(alpha * instant_rate + (1.0 - alpha) * self.ms_per_char, 1)

        # 基本オーバーヘッドの微調整 (短文の場合に強く影響)
        if text_len <= 5:
            self.base_latency_ms = round(alpha * max(20.0, synth_ms - text_len * self.ms_per_char) + (1.0 - alpha) * self.base_latency_ms, 1)

    def _maybe_apply_aging(self) -> None:
        """数時間アクセスのないノードプロファイルを事前基準値(Prior)へ緩やかに回帰 (時間的エージング)"""
        now = time.time()
        elapsed = now - self.last_updated
        if elapsed > 7200.0 and self.sample_count > 0:  # 2時間以上経過
            decay_steps = min(5, int(elapsed / 7200.0))
            eng_priors = ENGINE_INITIAL_PRIORS.get(self.engine_name, ENGINE_INITIAL_PRIORS.get("VOICEVOX", {}))
            target_p = eng_priors.get("cloud" if "cloud" in eng_priors else ("gpu_top" if self.has_gpu else "cpu"), {})
            if target_p:
                prior_rate = float(target_p.get("ms_per_char", 25.0))
                prior_base = float(target_p.get("base_ms", 120.0))
                factor = 0.9 ** decay_steps
                self.ms_per_char = round(self.ms_per_char * factor + prior_rate * (1.0 - factor), 1)
                self.base_latency_ms = round(self.base_latency_ms * factor + prior_base * (1.0 - factor), 1)
            self.last_updated = now

    def estimate(self, text_len: int) -> float:
        """指定文字数の予測合成時間をミリ秒で算出 (エージング反映)"""
        self._maybe_apply_aging()
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
        self._maybe_apply_aging()
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

    def record_synth_direct(self, engine_name: str, text_len: int, synth_ms: float, node_id: str = "cloud_direct") -> None:
        """クラウドTTSや直接実行エンジン等の実績を記録"""
        if not engine_name or text_len <= 0 or synth_ms <= 0:
            return
        eng_upper = engine_name.strip().upper()
        key = (node_id, eng_upper)
        if key not in self._profiles:
            self._profiles[key] = NodeLatencyProfile(
                node_id=node_id,
                engine_name=eng_upper,
                has_gpu=False,
                machine_name="Cloud/Direct"
            )
        self._profiles[key].record(text_len, synth_ms)
        logger.debug(
            f"📊 [Profiler] Updated direct {key[0]}/{key[1]}: "
            f"{text_len} chars in {synth_ms:.0f}ms -> rate: {self._profiles[key].ms_per_char:.1f}ms/char (base: {self._profiles[key].base_latency_ms:.0f}ms)"
        )

    def estimate_ms(self, node: Any, text_len: int) -> float:
        """指定ノードでの完了予測時間を取得"""
        profile = self.get_or_create(node)
        return profile.estimate(text_len)

    def estimate_synthesis_ms(
        self,
        engine: Optional[str] = None,
        text_len: int = 10,
        candidate_nodes: Optional[List[Any]] = None
    ) -> float:
        """
        指定エンジンと文字数から、現在のクラスタ推論プロファイルに基づく予測合成時間(ms)を算出。
        """
        safe_len = max(1, int(text_len))
        if not engine:
            return 100.0 + safe_len * 20.0

        eng_upper = engine.strip().upper()
        matched_profiles: List[NodeLatencyProfile] = []
        if candidate_nodes:
            for n in candidate_nodes:
                if getattr(n, "is_available", True):
                    p = self.get_or_create(n)
                    if p.engine_name == eng_upper:
                        matched_profiles.append(p)
        else:
            for (_nid, e_name), p in self._profiles.items():
                if e_name == eng_upper:
                    matched_profiles.append(p)

        if matched_profiles:
            gpu_profiles = [p for p in matched_profiles if p.has_gpu]
            if gpu_profiles:
                best_gpu = max(gpu_profiles, key=lambda p: p.gpu_power_score)
                return best_gpu.estimate(safe_len)
            best_cpu = max(matched_profiles, key=lambda p: (p.sample_count, -p.ms_per_char))
            return best_cpu.estimate(safe_len)

        eng_prior = ENGINE_INITIAL_PRIORS.get(eng_upper)
        if eng_prior:
            if "cloud" in eng_prior:
                prior = eng_prior["cloud"]
            elif "cpu" in eng_prior and not any(p.has_gpu for p in self._profiles.values()):
                prior = eng_prior["cpu"]
            else:
                has_any_gpu = any(p.has_gpu for p in self._profiles.values())
                prior = eng_prior.get("gpu_gen") or eng_prior.get("cpu", {"base_ms": 150.0, "ms_per_char": 30.0})
        else:
            prior = {"base_ms": 120.0, "ms_per_char": 20.0}
        return max(30.0, prior["base_ms"] + safe_len * prior["ms_per_char"])

    def calculate_dynamic_speculative_timeout(
        self,
        base_timeout: float = 2.5,
        engine: Optional[str] = None,
        text_len: int = 10,
        chunk_index: int = 0,
        buffer_headroom_sec: float = 0.0,
        candidate_nodes: Optional[List[Any]] = None
    ) -> float:
        """
        チャンク位置・先行再生バッファ残量・文字数・エンジン実測推論時間に応じた
        動的な Speculative TTS タイムアウト秒数を算出する。
        - base_timeout は「最大許容沈黙時間 (上限キャップ)」として機能 (0.8s 〜 6.0s)。
        - chunk_index == 0 (初動チャンク):
          推定推論時間×1.35倍を動的目標とし、下限0.8s、上限 cap_timeout でクランプ。
          これにより、VOICEVOX等の高速モデルは無駄に待たず最速（0.8〜1.2秒）でローカル判定され、
          COEIROINK等の重いモデルはユーザー許容上限（cap_timeout）まで粘り強くローカルを待つ。
        - chunk_index >= 1 (後続チャンク):
          先行チャンク群の再生猶予 (buffer_headroom_sec) からクラウドTTS合成マージン (0.45s) を引いた秒数まで
          ローカルエンジンの完了を粘り強く待ち、不必要な声変わり（クラウド投機混入）を防止する。
        """
        cap_timeout = max(0.8, min(6.0, float(base_timeout or 2.5)))
        est_ms = self.estimate_synthesis_ms(engine, max(1, text_len), candidate_nodes=candidate_nodes)
        est_sec = est_ms / 1000.0

        if chunk_index <= 0 and buffer_headroom_sec <= 0.2:
            target = est_sec * 1.35
            return round(min(cap_timeout, max(0.8, target)), 2)

        # 後続チャンクまたは十分な再生バッファ残量がある場合
        headroom_budget = max(0.0, float(buffer_headroom_sec) - 0.45)
        target = max(headroom_budget, est_sec * 1.5)
        upper_limit = max(cap_timeout, 4.0)
        return round(min(6.0, max(0.8, min(upper_limit, target))), 2)

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
        - クラウドTTS (edge, azure, google):
          - レートリミット保護や接続オーバーヘッドを抑えるため安全下限 (min_chars: 14文字) を保証。
          - 高速回線環境では自然なイントネーションを維持するため上限 (max_chars: 38文字) まで許容。
          - 初動目標 1400ms 以内に収まるよう実績 (EWMA) または事前値から逆算スケーリング。
        - ローカルTTS (coeiroink, voicevox, sharevox):
          - GPUノード / CPUノードの実績プロファイル (EWMA) に応じて 8〜30文字に動的スケーリング。
        - 軽量ルール (openjtalk):
          - 超高速・低負荷なため、14〜35文字の範囲で安定動作。
        """
        if not engine:
            return max_chars

        eng_lower = engine.strip().lower()
        eng_upper = eng_lower.upper()

        CLOUD_ENGINES = {"edge", "azure", "google"}
        is_cloud = eng_lower in CLOUD_ENGINES

        eff_min_chars = max(min_chars, 14) if is_cloud else min_chars
        eff_max_chars = max(max_chars, 38) if is_cloud else max_chars
        eff_target_budget_ms = 1400.0 if (is_cloud and target_budget_ms > 1400.0) else target_budget_ms

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
            eng_prior = ENGINE_INITIAL_PRIORS.get(eng_upper)
            if not eng_prior:
                return max_chars
            if "cloud" in eng_prior:
                prior = eng_prior["cloud"]
            elif "cpu" in eng_prior and not any(p.has_gpu for p in self._profiles.values()):
                prior = eng_prior["cpu"]
            else:
                has_any_gpu = any(p.has_gpu for p in self._profiles.values())
                prior = eng_prior.get("gpu_gen") or eng_prior.get("cpu", {"base_ms": 150.0, "ms_per_char": 30.0})

            avail_ms = max(0.0, eff_target_budget_ms - prior["base_ms"])
            chars = int(avail_ms / max(1.0, prior["ms_per_char"]))
            return max(eff_min_chars, min(chars, eff_max_chars))

        # クラウドプロファイルが存在する場合 (実績フィードバックに基づく動的逆算)
        if is_cloud:
            cloud_profile = matched_profiles[0]
            return cloud_profile.calc_max_chars(eff_target_budget_ms, eff_min_chars, eff_max_chars)

        # GPUノードがあれば最強GPUノードのプロファイルを採用
        gpu_profiles = [p for p in matched_profiles if p.has_gpu]
        if gpu_profiles:
            best_gpu = max(gpu_profiles, key=lambda p: p.gpu_power_score)
            return best_gpu.calc_max_chars(target_budget_ms, eff_min_chars, eff_max_chars)

        # CPUノードのみの場合: サンプル数が多く安定したプロファイルを採用
        cpu_profiles = matched_profiles
        if cpu_profiles:
            best_cpu = max(cpu_profiles, key=lambda p: (p.sample_count, -p.ms_per_char))
            return best_cpu.calc_max_chars(target_budget_ms, eff_min_chars, eff_max_chars)

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

    def restore_from_history(self, records: List[Dict[str, Any]]) -> int:
        """再起動時に過去の再生履歴から各ノード・プロバイダーの推論プロファイルを自動ウォームスタート"""
        restored = 0
        for r in records:
            chunks = r.get("chunks") or []
            for c in chunks:
                if c.get("is_cached"):
                    continue
                s_ms = float(c.get("synth_ms") or c.get("synthesis_ms") or 0.0)
                if s_ms <= 0:
                    continue
                raw_len = int(c.get("char_count") or len(c.get("text", "") or "") or 0)
                if raw_len <= 0:
                    continue
                prov = str(c.get("provider", "")).lower()
                w = str(c.get("worker", "") or c.get("worker_label", ""))

                if prov in ("edge", "azure", "google", "openjtalk"):
                    self.record_synth_direct(prov, raw_len, s_ms)
                    restored += 1
                elif w:
                    class _DummyNode:
                        def __init__(self, wid: str, eng: str, wlbl: str):
                            self.id = wid
                            self.engine_name = eng.upper()
                            self.machine_name = wlbl
                            self.has_gpu = any(k in wlbl.lower() for k in ("gpu", "rtx", "gtx", "cuda", "radeon"))
                            self.specs = {"gpu": self.has_gpu}
                    dn = _DummyNode(w, prov, w)
                    self.record_synth(dn, raw_len, s_ms)
                    restored += 1
        if restored > 0:
            logger.info(f"✨ Restored {restored} chunk profiles into EngineLatencyProfiler from history.")
        return restored

# グローバルシングルトンインスタンス
latency_profiler = EngineLatencyProfiler()
