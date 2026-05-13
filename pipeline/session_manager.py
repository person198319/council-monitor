"""
P2 - 質詢 Session 管理（狀態機）

狀態：
  IDLE      → 等待觸發
  RECORDING → 累積逐字稿，直到靜默逾時

完結條件：靜默超過 silence_timeout_sec（主迴圈呼叫 check_timeout()）
最短時限：session 未達 min_duration_sec 則丟棄（過濾誤報）
"""
import time
from enum import Enum, auto
from datetime import datetime
from loguru import logger

from pipeline.keyword_detector import KeywordDetector


class _State(Enum):
    IDLE = auto()
    RECORDING = auto()


class SessionManager:
    def __init__(self, cfg: dict):
        self.detector = KeywordDetector(cfg)
        self.silence_timeout: int = cfg["session"]["silence_timeout_sec"]
        self.min_duration: int = cfg["session"]["min_duration_sec"]

        self._state = _State.IDLE
        self._start_wall: float = 0.0
        self._start_ts: str = ""
        self._chunks: list[str] = []
        self._last_text_wall: float = 0.0
        self._trigger_conf: float = 0.0

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def on_text(self, result: dict) -> dict | None:
        """
        每收到一段逐字稿呼叫一次。
        回傳已完結的 session dict，或 None（session 尚未結束）。
        """
        triggered, confidence = self.detector.check(result)
        now = time.time()

        if self._state == _State.IDLE:
            if triggered:
                self._start(result["text"], confidence, now)

        elif self._state == _State.RECORDING:
            self._chunks.append(result["text"])
            self._last_text_wall = now
            # 繼續跑 detector，更新滑動視窗（不重複觸發）

        return None  # 完結由 check_timeout() 偵測

    def check_timeout(self) -> dict | None:
        """
        主迴圈每個 chunk 結束後呼叫，偵測靜默逾時。
        回傳完結的 session dict，或 None。
        """
        if self._state != _State.RECORDING:
            return None
        if time.time() - self._last_text_wall >= self.silence_timeout:
            return self._end()
        return None

    @property
    def is_recording(self) -> bool:
        return self._state == _State.RECORDING

    # ------------------------------------------------------------------
    # 內部方法
    # ------------------------------------------------------------------

    def _start(self, first_text: str, confidence: float, now: float) -> None:
        self._state = _State.RECORDING
        self._start_wall = now
        self._start_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._chunks = [first_text]
        self._last_text_wall = now
        self._trigger_conf = confidence
        logger.info(
            f"🔴 Session 開始｜信心 {confidence:.2f}｜"
            f"{first_text[:40]}..."
        )

    def _end(self) -> dict | None:
        duration = time.time() - self._start_wall

        if duration < self.min_duration:
            logger.info(
                f"⚠️  Session 太短（{duration:.0f}s < {self.min_duration}s），忽略"
            )
            self._reset()
            return None

        session = {
            "timestamp": self._start_ts,
            "duration_sec": round(duration),
            "transcript": "\n".join(self._chunks),
            "chunk_count": len(self._chunks),
            "trigger_confidence": self._trigger_conf,
        }
        logger.info(
            f"✅ Session 完結｜{duration:.0f}s｜"
            f"{len(self._chunks)} 段逐字稿"
        )
        self._reset()
        return session

    def _reset(self) -> None:
        self._state = _State.IDLE
        self._chunks = []
        self._start_wall = 0.0
        self._last_text_wall = 0.0
        self._trigger_conf = 0.0
