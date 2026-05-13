"""
P2 - 三層關鍵詞偵測
Layer 1: 高信心詞直接觸發
Layer 2: 語法分析判斷質詢者 vs 回答者
Layer 3: 低信心詞需多詞共現

設計原則：寧可多觸發（召回率優先），誤報由 SessionManager 的最小時長過濾。
"""
import re
from collections import deque
from loguru import logger


class KeywordDetector:
    def __init__(self, cfg: dict):
        self.high_conf: list[str] = cfg["keywords"]["high_confidence"]
        self.low_conf: list[str] = cfg["keywords"]["low_confidence"]
        # targets 可能有空字串，過濾掉
        self.targets: list[str] = [t for t in cfg["keywords"]["targets"] if t]

        # 滑動文字視窗（保留最近 2 分鐘的逐字稿）
        self._window: deque[dict] = deque()
        self._window_sec: int = 120

        # 語法模式
        self._question_patterns = [
            r"請問.{0,10}(局長|院長)",
            r"(局長|院長).{0,5}(怎麼|如何|為何|為什麼|有沒有|有無)",
            r"(請|麻煩).{0,5}(局長|院長).{0,5}(說明|解釋|回答|報告)",
            r"(局長|院長).{0,10}[嗎呢啊？?]",
            r"(委員|議員).{0,10}(問|詢問|指出|表示)",
        ]
        self._answer_patterns = [
            r"^(是的|好的|謝謝|報告|我們)",
            r"(我們已經|本局|本院|目前規劃|本市)",
            r"(感謝|謝謝).{0,5}(委員|議員)",
            r"(將會|會再|持續|已於).{0,10}(辦理|改善|處理|執行)",
        ]

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def check(self, result: dict) -> tuple[bool, float]:
        """
        輸入一段逐字稿 result，回傳 (是否觸發, 信心分數 0–1)。
        同時更新滑動視窗。
        """
        self._update_window(result)
        window_text = self._window_text()
        current_text = result["text"]

        # Layer 1：高信心詞或官員姓名
        high_hits = [kw for kw in self.high_conf if kw in window_text]
        target_hits = [t for t in self.targets if t in window_text]

        if high_hits or target_hits:
            role = self._classify_role(window_text)
            triggered_kws = high_hits + target_hits
            logger.debug(f"Layer1 命中：{triggered_kws}，角色判斷：{role}")

            if role == "questioner":
                return True, 0.9
            elif role == "responder":
                # 局長在回答，不是被質詢的當下，不觸發
                return False, 0.0
            else:
                # 模糊：保守觸發，降低信心
                return True, 0.6

        # Layer 2：低信心詞需兩個以上共現
        low_hits = [kw for kw in self.low_conf if kw in window_text]
        if len(low_hits) >= 2:
            role = self._classify_role(window_text)
            logger.debug(f"Layer2 命中：{low_hits}，角色判斷：{role}")
            if role != "responder":
                return True, 0.5

        return False, 0.0

    # ------------------------------------------------------------------
    # 內部方法
    # ------------------------------------------------------------------

    def _update_window(self, result: dict) -> None:
        self._window.append(result)
        # 移除超出視窗的舊片段（以音訊時間戳計算）
        if len(self._window) > 1:
            latest_start = self._window[-1]["start"]
            cutoff = latest_start - self._window_sec
            while self._window and self._window[0]["start"] < cutoff:
                self._window.popleft()

    def _window_text(self) -> str:
        return " ".join(r["text"] for r in self._window)

    def _classify_role(self, text: str) -> str:
        """
        判斷目前說話者是質詢者（議員）還是回答者（局長/院長）。
        回傳 'questioner' | 'responder' | 'uncertain'
        """
        q_score = sum(1 for p in self._question_patterns if re.search(p, text))
        a_score = sum(1 for p in self._answer_patterns if re.search(p, text))

        if q_score > a_score:
            return "questioner"
        if a_score > q_score:
            return "responder"
        return "uncertain"
