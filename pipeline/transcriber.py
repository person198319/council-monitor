"""
P1 - faster-whisper 即時逐字稿
producer-consumer 架構：(音訊, offset) queue → 轉錄 → 結果 queue

chunk_offset_sec 是該 chunk 的直播絕對起始秒數，
加上 Whisper 輸出的相對時間後得到影片的絕對時間戳。
"""
import queue
import threading
import numpy as np
from faster_whisper import WhisperModel
from loguru import logger
from data.corrections import post_correct


class Transcriber:
    def __init__(
        self,
        model_size: str = "large-v3",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "zh",
        num_workers: int = 4,
        initial_prompt: str = "",
    ):
        logger.info(f"載入 Whisper {model_size}（{device}/{compute_type}）...")
        self.model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            num_workers=num_workers,
        )
        self.language = language
        self.initial_prompt = initial_prompt

        self._audio_q: queue.Queue[tuple[np.ndarray, float] | None] = queue.Queue(maxsize=10)
        self._result_q: queue.Queue[dict] = queue.Queue()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        logger.info("Transcriber 啟動完成")

    def feed(self, audio: np.ndarray, chunk_offset_sec: float = 0.0) -> None:
        """送入一段音訊與對應的直播時間偏移（非阻塞，若 queue 滿則丟棄並警告）"""
        try:
            self._audio_q.put((audio, chunk_offset_sec), timeout=2)
        except queue.Full:
            logger.warning("轉錄 queue 已滿，丟棄一段音訊（推論跟不上串流速度）")

    def get_results(self) -> list[dict]:
        """取出所有已完成的轉錄結果（非阻塞）"""
        results = []
        while not self._result_q.empty():
            results.append(self._result_q.get_nowait())
        return results

    def stop(self) -> None:
        self._audio_q.put(None)
        self._worker.join()

    def _run(self) -> None:
        while True:
            item = self._audio_q.get()
            if item is None:
                break
            audio, chunk_offset = item
            self._transcribe(audio, chunk_offset)

    def _transcribe(self, audio: np.ndarray, chunk_offset: float) -> None:
        try:
            segments, _ = self.model.transcribe(
                audio,
                language=self.language,
                initial_prompt=self.initial_prompt,
                beam_size=5,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                word_timestamps=True,
            )

            for seg in segments:
                text = post_correct(seg.text.strip())
                if not text:
                    continue

                # 加上 chunk 起始偏移，換算成直播絕對時間
                abs_start = round(chunk_offset + seg.start, 1)
                abs_end   = round(chunk_offset + seg.end,   1)

                low_conf_words = [
                    w.word for w in seg.words if w.probability < 0.4
                ] if seg.words else []

                result = {
                    "text":           text,
                    "start":          abs_start,
                    "end":            abs_end,
                    "low_conf_words": low_conf_words,
                }
                self._result_q.put(result)
                logger.debug(f"[{abs_start:.1f}s] {text}")

        except Exception as e:
            logger.error(f"轉錄失敗：{e}")
