"""
說話者追蹤器（文字型，不需要音訊 diarization）

運作方式：
1. 每句話進來時，用語法模式即時推斷角色（議員/官員/主席）
2. 同時偵測已知人物是否被點名或自我指涉
3. 一旦確認身份，自動回填該角色的近期發言
4. 未識別者以角色標籤（議員/官員/不明）顯示

後續可加入 pyannote 音訊 diarization 取代文字型角色推斷。
"""
import re
from enum import Enum
from loguru import logger
from data.known_speakers import ALIAS_TO_SPEAKER, KNOWN_SPEAKERS


class SpeakerRole(Enum):
    COUNCILOR = "議員"
    OFFICIAL  = "官員"
    CHAIR     = "主席"
    UNKNOWN   = "不明"


_COUNCILOR_PATTERNS = [
    r"請問.{0,10}(局長|院長)",
    r"(局長|院長).{0,5}(怎麼|如何|為何|有沒有)",
    r"(我|本席).{0,5}(要問|想問|質詢)",
    r"(你們|貴局|貴院).{0,5}(有沒有|是否|為什麼)",
    r"請.{0,5}(說明|解釋|回答|提出)",
    r"(剛才|剛剛|前面).{0,5}(說|提到|承諾).{0,10}(但|卻|還)",
]

_OFFICIAL_PATTERNS = [
    r"^(是的|好的|報告|謝謝委員|感謝|遵照)",
    r"(本局|本院|我們局|我們院).{0,5}(已經|目前|將會|規劃|辦理)",
    r"(感謝|謝謝).{0,5}(委員|議員)",
    r"向.{0,5}(委員|議員).{0,5}(報告|說明)",
    r"我們.{0,5}(聯醫|聯合醫院|衛生局)",
]

_CHAIR_PATTERNS = [
    r"請.{0,5}(局長|院長|議員).{0,5}(發言|說明|回答)",
    r"(現在|接下來)進行質詢",
    r"(時間|時限).{0,5}(已到|屆滿)",
]


def infer_role(text: str) -> SpeakerRole:
    if any(re.search(p, text) for p in _CHAIR_PATTERNS):
        return SpeakerRole.CHAIR
    if any(re.search(p, text) for p in _OFFICIAL_PATTERNS):
        return SpeakerRole.OFFICIAL
    if any(re.search(p, text) for p in _COUNCILOR_PATTERNS):
        return SpeakerRole.COUNCILOR
    return SpeakerRole.UNKNOWN


class SpeakerTracker:
    """
    追蹤每段逐字稿的說話者角色與身份。
    每個 session 開始時 reset()，結束時可取出帶標注的 segments。
    """

    def __init__(self):
        self._segments: list[dict] = []

    def annotate(self, result: dict) -> dict:
        """
        輸入 result dict（含 text/start/end），
        回傳加上 role/speaker_name 欄位的新 dict。
        """
        text = result["text"]
        role = infer_role(text)
        name = self._instant_identify(text, role)

        enriched = {**result, "role": role.value, "speaker_name": name}
        self._segments.append(enriched)

        if name:
            self._backfill_role(name, role)

        return enriched

    def reset(self) -> None:
        self._segments.clear()

    # ------------------------------------------------------------------
    # 內部識別邏輯
    # ------------------------------------------------------------------

    def _instant_identify(self, text: str, role: SpeakerRole) -> str:
        """
        三種識別情境（按優先序）：
        A. 自我指涉用詞（「本院」「本局」）+ 官員語氣
        B. 上一句點名了某人，這句是官員回應
        C. 直接出現人名（如逐字稿中出現「王智弘院長表示」）
        """
        # 情境 A：自我指涉
        if role in (SpeakerRole.OFFICIAL, SpeakerRole.UNKNOWN):
            for known in sorted(KNOWN_SPEAKERS, key=lambda x: x.priority):
                for ref in known.self_ref:
                    if ref in text:
                        logger.info(f"[識別-A] 自我指涉「{ref}」→ {known.name}")
                        return known.name

        # 情境 B：上一句點名，這句是官員回應
        if role == SpeakerRole.OFFICIAL and self._segments:
            prev_text = self._segments[-1]["text"]
            for alias, known in sorted(
                ALIAS_TO_SPEAKER.items(), key=lambda x: x[1].priority
            ):
                if alias in prev_text:
                    logger.info(f"[識別-B] 上句點名「{alias}」→ 回應者是 {known.name}")
                    return known.name

        # 情境 C：這句話直接提及人名（通常是逐字稿中有旁白式說明）
        for alias, known in sorted(
            ALIAS_TO_SPEAKER.items(), key=lambda x: x[1].priority
        ):
            pattern = f"{alias}(表示|說|指出|回應|補充)"
            if re.search(pattern, text):
                logger.info(f"[識別-C] 文中提及「{alias}」→ {known.name}")
                return known.name

        return ""

    def _backfill_role(self, name: str, role: SpeakerRole) -> None:
        """向前回填：將相同角色且尚未命名的連續發言填入剛確認的姓名"""
        count = 0
        for seg in reversed(self._segments[:-1]):
            if seg["speaker_name"]:
                break
            if seg["role"] in (role.value, SpeakerRole.UNKNOWN.value):
                seg["speaker_name"] = name
                count += 1
        if count:
            logger.info(f"[識別] 回填 {count} 句 → {name}")
