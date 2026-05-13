"""
P1 - faster-whisper 即時逐字稿
producer-consumer 架構：音訊 queue → 轉錄 → 結果 queue
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

        self._audio_q: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=10)
        self._result_q: queue.Queue[dict] = queue.Queue()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        logger.info("Transcriber 啟動完成")

    def feed(self, audio: np.ndarray) -> None:
        """送入一段音訊（非阻塞，若 queue 滿則丟棄並警告）"""
        try:
            self._audio_q.put(audio, timeout=2)
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
            audio = self._audio_q.get()
            if audio is None:
                break
            self._transcribe(audio)

    def _transcribe(self, audio: np.ndarray) -> None:
        try:
            segments, info = self.model.transcribe(
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

                # 低信心詞標記
                low_conf_words = [
                    w.word for w in seg.words if w.probability < 0.4
                ] if seg.words else []

                result = {
                    "text": text,
                    "start": round(seg.start, 1),
                    "end": round(seg.end, 1),
                    "low_conf_words": low_conf_words,
                }
                self._result_q.put(result)
                logger.debug(f"[{seg.start:.1f}s] {text}")

        except Exception as e:
            logger.error(f"轉錄失敗：{e}")
