"""
ストリーミング再生の先頭パーツ長プランナー。

先頭本文パーツの文字数候補ごとに「割当ノード → 合成完了 → 再生タイムライン基準の投機 → 再生」を予測し、
「最初の音 ≤ 最大許容沈黙」かつ「途切れ 0」かつ「声変わり (クラウド投機採用) 0」を満たす最長の文字数を選ぶ。
GPU が空いていれば通常長のまま (細切れにしない)、CPU しか空いていない等で間に合わない時だけ短くする。

予測の合成時間には悲観係数を掛ける (Profiler は成功サンプルのみ学習するため楽観寄りに偏る)。
検証: scratch/exp_sim9.py (実測回帰ベースのシミュレーション)
"""
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

LOCAL_PLAY_SEC_PER_CHAR = 0.14  # ローカル再生長の保守側単価 (COEIROINK 実測 0.14〜0.21)
CLOUD_PLAY_SEC_PER_CHAR = 0.12  # クラウド再生長の下限 (Azure 実測 0.12〜0.15)
CLOUD_LATENCY_SEC = 0.27        # クラウド投機の応答 (Azure 実測 p50≈0.2s, 事故時 0.27s)
PART_PAUSE_SEC = 0.06
PREROLL_SAFETY_SEC = 0.25       # Adaptive Preroll (名前あり) の安全マージン
PREROLL_MIN_WAIT_SEC = 0.15
HEAD_MIN_CHARS = 10             # _hardcap_slice の最小スライス長 (_MIN_PART_CHARS*2)


@dataclass
class PlanResult:
    first_sound_s: float
    gap_s: float
    cloud_count: int


def _simulate(
    lens: List[int],
    has_name: bool,
    name_play_s: float,
    nodes: List[Dict[str, Any]],
    max_silence: float,
    az_margin: float,
    k_plan: float,
) -> PlanResult:
    pending = [float(n["pending_ms"]) * k_plan / 1000.0 for n in nodes]
    est: List[Callable[[int], float]] = [n["est_ms"] for n in nodes]

    ready_local: List[float] = []
    for i, L in enumerate(lens):
        # 先頭は完了予測最短ノード、後続も貪欲に完了予測最短ノードへ (ルーティングの近似)
        best = min(range(len(nodes)), key=lambda j: pending[j] + est[j](L) * k_plan / 1000.0)
        pending[best] += est[best](L) * k_plan / 1000.0
        ready_local.append(pending[best])

    cloud = 0

    def resolve(i: int, deadline: float) -> float:
        nonlocal cloud
        if ready_local[i] < deadline + CLOUD_LATENCY_SEC:
            return ready_local[i]
        cloud += 1
        return deadline + CLOUD_LATENCY_SEC

    def play(L: int, is_cloud: bool) -> float:
        return L * (CLOUD_PLAY_SEC_PER_CHAR if is_cloud else LOCAL_PLAY_SEC_PER_CHAR) + PART_PAUSE_SEC

    if has_name:
        r1 = resolve(0, max(0.3, max_silence + name_play_s - az_margin))
        head_cloud = r1 != ready_local[0]
        need = max(r1 + PREROLL_SAFETY_SEC - name_play_s, PREROLL_MIN_WAIT_SEC)
        wait = max(0.0, min(need, max_silence))
        first = min(r1, wait) if wait > 0.03 else 0.0
        end = first + name_play_s
        st = max(end, r1)
        gap = st - end
        end = st + play(lens[0], head_cloud)
    else:
        r0 = resolve(0, max(0.3, max_silence - az_margin))
        first = r0
        gap = 0.0
        end = first + play(lens[0], r0 != ready_local[0])

    for i in range(1, len(lens)):
        ri = resolve(i, max(0.3, end - az_margin))
        st = max(end, ri)
        gap += st - end
        end = st + play(lens[i], ri != ready_local[i])
    return PlanResult(first_sound_s=first, gap_s=gap, cloud_count=cloud)


def plan_head_chars(
    body_lens: List[int],
    has_name: bool,
    name_play_s: float,
    nodes: List[Dict[str, Any]],
    max_silence: float,
    az_margin: float,
    k_plan: float = 1.5,
    min_chars: int = HEAD_MIN_CHARS,
) -> Optional[int]:
    """
    先頭本文パーツの文字数を決める。短縮不要 (通常長のままで条件を満たす) なら None。
    body_lens: 本文パーツの文字数 (再生順、先頭が対象)
    nodes: [{"pending_ms": float, "est_ms": Callable[[int], float]}] (完了予測の材料)
    """
    if not body_lens or not nodes or body_lens[0] <= min_chars:
        return None
    full = body_lens[0]
    rest = body_lens[1:]
    best_ok: Optional[int] = None
    fallback = None
    for L in range(min_chars, full + 1):
        lens = [L] + ([full - L] if full - L > 0 else []) + rest
        r = _simulate(lens, has_name, name_play_s, nodes, max_silence, az_margin, k_plan)
        if r.cloud_count == 0 and r.gap_s < 0.001 and r.first_sound_s <= max_silence:
            best_ok = L
        key = (r.cloud_count, round(r.gap_s, 3), r.first_sound_s)
        if fallback is None or key < fallback[0]:
            fallback = (key, L)
    chosen = best_ok if best_ok is not None else fallback[1]
    return None if chosen >= full else chosen
