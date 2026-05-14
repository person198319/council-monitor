"""
P2 - 質詢 Session 管理（狀態機）

狀態：
  IDLE      → 等待觸發
  RECORDING → 累積逐字稿，直到靜默逾時

完結後產出：
  transcript            - 純文字（供 LLM 摘要）
  timestamped_transcript - 帶 [MM:SS] 標籤與說話者（供存檔）
  segments              - 原始 result dict 列表（供進階查詢）
  video_start_sec / video_end_sec - 直播時間範圍
"""
import time
from enum import Enum, auto
from datetime import datetime
from loguru import logger

from pipeline.keyword_detector import KeywordDetector
from pipeline.speaker_tracker import SpeakerTracker


class _State(Enum):
    IDLE = auto()
    RECORDING = auto()


def _fmt(sec: float) -> str:
    """秒數轉 MM:SS 或 HH:MM:SS"""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class SessionManager:
    def __init__(self, cfg: dict):
        self.detector = KeywordDetector(cfg)
        self.silence_timeout: int = cfg["session"]["silence_timeout_sec"]
        self.min_duration: int = cfg["session"]["min_duration_sec"]
        self.speaker_tracker = SpeakerTracker()

        self._state = _State.IDLE
        self._start_wall: float = 0.0
        self._start_ts: str = ""
        self._chunks: list[dict] = []
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

        # 說話者標注（每句都做，IDLE 時也追蹤上下文）
        enriched = self.speaker_tracker.annotate(result)

        if self._state == _State.IDLE:
            if triggered:
                self._start(enriched, confidence, now)

        elif self._state == _State.RECORDING:
            self._chunks.append(enriched)
            self._last_text_wall = now

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

    def _start(self, first_result: dict, confidence: float, now: float) -> None:
        self._state = _State.RECORDING
        self._start_wall = now
        self._start_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._chunks = [first_result]
        self._last_text_wall = now
        self._trigger_conf = confidence
        logger.info(
            f"🔴 Session 開始｜信心 {confidence:.2f}｜"
            f"影片 {_fmt(first_result['start'])}｜"
            f"{first_result['text'][:40]}..."
        )

    def _end(self) -> dict | None:
        duration = time.time() - self._start_wall

        if duration < self.min_duration:
            logger.info(f"⚠️  Session 太短（{duration:.0f}s < {self.min_duration}s），忽略")
            self._reset()
            return None

        # 純文字逐字稿（供 LLM 摘要）
        plain_transcript = "\n".join(c["text"] for c in self._chunks)

        # 帶時間戳與說話者的逐字稿（供存檔）
        lines = []
        for c in self._chunks:
            time_label = f"[{_fmt(c['start'])}–{_fmt(c['end'])}]"
            speaker = c.get("speaker_name") or c.get("role", "")
            speaker_label = f"[{speaker}]" if speaker and speaker != "不明" else ""
            lines.append(f"{time_label}{speaker_label} {c['text']}")
        timestamped_transcript = "\n".join(lines)

        video_start = self._chunks[0]["start"] if self._chunks else 0.0
        video_end   = self._chunks[-1]["end"]   if self._chunks else 0.0

        session = {
            "timestamp":              self._start_ts,
            "duration_sec":           round(duration),
            "transcript":             plain_transcript,
            "timestamped_transcript": timestamped_transcript,
            "segments":               list(self._chunks),
            "chunk_count":            len(self._chunks),
            "trigger_confidence":     self._trigger_conf,
            "video_start_sec":        video_start,
            "video_end_sec":          video_end,
        }
        logger.info(
            f"✅ Session 完結｜{duration:.0f}s｜"
            f"影片 {_fmt(video_start)}–{_fmt(video_end)}｜"
            f"{len(self._chunks)} 段"
        )
        self._reset()
        return session

    def _reset(self) -> None:
        self._state = _State.IDLE
        self._chunks = []
        self._start_wall = 0.0
        self._last_text_wall = 0.0
        self._trigger_conf = 0.0
        self.speaker_tracker.reset()
