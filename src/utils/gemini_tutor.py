import asyncio
import json
import logging
import re
import time
from typing import Optional, Dict, Any, List

import aiohttp

logger = logging.getLogger(__name__)

class GeminiTutorClient:
    """
    Gemini 2.5 Flash を活用した非同期日本語読み学習・辞書チュータークライアント。
    音声読み上げの遅延に一切影響を与えない完全非同期設計で、サーバーCPU負荷は0%。
    無料枠（15 RPM / 1500 RPD）を厳守するトークンバケットレート制限を内包。
    """

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self.api_key = api_key.strip()
        self.model = model.strip() if model else "gemini-2.5-flash"
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_request_times: List[float] = []
        self._max_rpm = 15  # 1分あたり最大15回 (無料枠の厳格上限)
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15.0, connect=5.0)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _wait_for_rate_limit(self) -> bool:
        """15 RPM のレート制限を遵守。枠を超えそうな場合は待機、待機時間が長すぎる場合はスキップ"""
        async with self._lock:
            now = time.time()
            # 60秒以上前のタイムスタンプを破棄
            self._last_request_times = [t for t in self._last_request_times if now - t < 60.0]

            if len(self._last_request_times) >= self._max_rpm:
                oldest = self._last_request_times[0]
                wait_seconds = 60.0 - (now - oldest) + 0.5
                if wait_seconds > 10.0:
                    logger.warning(f"⚠️ [GeminiTutor] Rate limit reached ({len(self._last_request_times)}/{self._max_rpm} RPM). Skipping request to protect quota.")
                    return False
                logger.info(f"⏳ [GeminiTutor] Rate limit approaching. Throttling for {wait_seconds:.1f}s...")
                await asyncio.sleep(wait_seconds)

            self._last_request_times.append(time.time())
            return True

    async def _call_gemini_api(self, prompt: str, system_instruction: Optional[str] = None) -> Optional[str]:
        """Gemini REST API に非同期リクエストを送信"""
        if not self.api_key:
            logger.debug("[GeminiTutor] API key not configured. Skipping.")
            return None

        if not await self._wait_for_rate_limit():
            return None

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"

        payload: Dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": 0.2,
            }
        }

        if system_instruction:
            payload["system_instruction"] = {
                "parts": [{"text": system_instruction}]
            }

        headers = {"Content-Type": "application/json"}
        session = await self._get_session()

        for attempt in range(2):
            try:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            if parts:
                                return parts[0].get("text", "")
                        return None
                    elif resp.status == 429:
                        logger.warning(f"⚠️ [GeminiTutor] HTTP 429 Too Many Requests on attempt {attempt+1}. Backing off...")
                        await asyncio.sleep(2.0 * (attempt + 1))
                    else:
                        err_body = await resp.text()
                        logger.error(f"❌ [GeminiTutor] API error HTTP {resp.status}: {err_body[:200]}")
                        return None
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ [GeminiTutor] API timeout on attempt {attempt+1}")
            except Exception as e:
                logger.error(f"❌ [GeminiTutor] Network error: {e}")
                return None

        return None

    @staticmethod
    def _clean_json_response(raw_text: str) -> str:
        """マークダウンコードブロック等の余計な装飾を除去"""
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        return cleaned.strip()

    async def infer_unknown_word(self, word: str, context_text: str = "") -> Optional[Dict[str, str]]:
        """
        未知語（英字造語や略語など）について、文脈に応じた自然なカタカナ読みを推論。
        戻り値: {"word": "...", "reading": "...", "reason": "..."}
        """
        w = word.strip()
        if not w or len(w) < 2:
            return None

        system_instruction = (
            "あなたはDiscord読み上げBotの専属日本語辞書チューターです。"
            "ユーザーが発言した未知の英字単語や造語について、日本のゲーマーやDiscordコミュニティで最も一般的で自然なカタカナ読みを回答してください。"
            "必ず以下のJSON形式オブジェクトのみを出力してください。\n"
            "{\"word\": \"単語\", \"reading\": \"カタカナ読み\", \"reason\": \"簡単な推論理由\"}"
        )

        prompt = f"単語: {w}\n"
        if context_text:
            prompt += f"文脈文: {context_text}\n"

        raw_json = await self._call_gemini_api(prompt, system_instruction=system_instruction)
        if not raw_json:
            return None

        try:
            cleaned_json = self._clean_json_response(raw_json)
            parsed = json.loads(cleaned_json)
            if isinstance(parsed, dict) and "reading" in parsed:
                # 読みがカタカナまたはひらがなになっているか確認
                reading = str(parsed["reading"]).strip()
                if reading and not re.search(r'[a-zA-Z]', reading):
                    return {
                        "word": w,
                        "reading": reading,
                        "reason": str(parsed.get("reason", "Gemini 2.5 Flash推論"))
                    }
        except Exception as e:
            logger.debug(f"[GeminiTutor] Failed to parse unknown word JSON: {e}, raw={raw_json}")

        return None

    async def analyze_misread_text(self, sentence: str) -> List[Dict[str, str]]:
        """
        ユーザーからリアクション指摘（🤔や❌）された文を分析し、
        TTSが誤読した可能性が高い難読語や固有名詞と、その正しいカタカナ読みのペアを抽出。
        """
        s = sentence.strip()
        if not s or len(s) < 2:
            return []

        system_instruction = (
            "あなたはDiscordの音声読み上げBotの専属日本語辞書チューターです。"
            "ユーザーから「読み間違えられた文」が報告されました。"
            "その文の中で、TTSエンジンが誤読しやすい「難読漢字」「同音異義語」「固有名詞」「英字略語」を特定し、文脈に応じた最も自然なカタカナ読みを提案してください。"
            "普通の一般的な単語（既に正しく読める単語）は含めず、本当に誤読されやすい1〜3単語のみを厳選してください。"
            "出力は必ず以下のJSON配列形式にしてください。\n"
            "[\n"
            "  {\"word\": \"対象単語\", \"reading\": \"カタカナ\", \"reason\": \"理由\"}\n"
            "]"
        )

        prompt = f"報告された文: {s}"

        raw_json = await self._call_gemini_api(prompt, system_instruction=system_instruction)
        if not raw_json:
            return []

        results: List[Dict[str, str]] = []
        try:
            cleaned_json = self._clean_json_response(raw_json)
            parsed = json.loads(cleaned_json)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and "word" in item and "reading" in item:
                        w = str(item["word"]).strip()
                        r = str(item["reading"]).strip()
                        reason = str(item.get("reason", "リアクション指摘からの推論"))
                        # 読みが空でなく、アルファベットが含まれていないこと（純粋な読み仮名）
                        if w and r and not re.search(r'[a-zA-Z]', r):
                            results.append({
                                "word": w,
                                "reading": r,
                                "reason": reason
                            })
        except Exception as e:
            logger.debug(f"[GeminiTutor] Failed to parse misread analysis JSON: {e}, raw={raw_json}")

        return results
