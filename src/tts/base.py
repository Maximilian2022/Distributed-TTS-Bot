from abc import ABC, abstractmethod

class TTSError(Exception):
    """TTS共通エラー"""
    pass

class RateLimitError(TTSError):
    """429クォータ超過・レートリミットエラー"""
    pass

class TTSConnectionError(TTSError):
    """TTS接続エラー・タイムアウト"""
    pass

# 後方互換用エイリアス
ConnectionError = TTSConnectionError

class BaseTTSClient(ABC):
    """
    TTSプロバイダー用抽象基底クラス (Adapterパターン)
    将来的なエンジン (VOICEVOX, COEIROINK等) の追加にも対応
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """プロバイダー名"""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """APIキーや認証情報が設定されており利用可能か"""
        pass

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        voice_name: str,
        pitch: str = "0%",
        speed: float = 1.0
    ) -> bytes:
        """
        テキストを音声バイナリ (WAV形式) に変換
        :param text: 読み上げテキスト
        :param voice_name: プロバイダー固有のボイス識別子
        :param pitch: ピッチ設定 (例: '+10%', '-5%', '0%')
        :param speed: 読み上げ速度 (例: 1.0, 1.2)
        :return: WAV形式の音声バイナリデータ
        """
        pass
