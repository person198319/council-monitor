"""
P3 - Gemma 27B 議會質詢摘要生成

流程：
  逐字稿 → Ollama (Gemma 27B) → 結構化 JSON → Pydantic 驗證 → SessionSummary
  失敗時最多重試 3 次，仍失敗回傳 None 並記錄 log。
"""
import json
import re
from datetime import datetime
from typing import Literal

from ollama import Client
from pydantic import BaseModel, field_validator
from loguru import logger


# ------------------------------------------------------------------
# Pydantic 驗證模型
# ------------------------------------------------------------------

class SessionSummary(BaseModel):
    議員姓名: str
    質詢對象: Literal["衛生局長", "聯醫院長", "兩者", "不明"]
    質詢主題: str
    質詢摘要: str
    局方回應: str
    承諾事項: list[str]
    待辦追蹤: list[str]
    緊急程度: Literal["立即", "本週", "本月", "追蹤"]
    相關單位: list[str]
    關鍵數字: dict[str, str]
    情緒張力: Literal["平和", "追問", "激烈"]

    @field_validator("質詢主題")
    @classmethod
    def title_not_too_long(cls, v: str) -> str:
        if len(v) > 25:
            return v[:25]
        return v

    @field_validator("質詢摘要")
    @classmethod
    def summary_not_empty(cls, v: str) -> str:
        if len(v) < 20:
            raise ValueError(f"摘要過短（{len(v)} 字），可能解析失敗")
        return v


# ------------------------------------------------------------------
# Prompt 常數
# ------------------------------------------------------------------

_SYSTEM = """你是台北市政府的議會質詢記錄專員。
將議會質詢逐字稿整理為結構化摘要，供衛生局與聯合醫院內部使用。

規則：
- 使用繁體中文
- 保持中立客觀
- 數字、日期、金額原文照錄
- 議員姓名不確定時標記「議員（姓名待確認）」
- 回覆純 JSON，不加其他說明文字"""

_USER_TMPL = """【質詢時間】{timestamp}
【逐字稿】
{transcript}

請整理為以下 JSON 格式：
{{
  "議員姓名": "姓名或議員（姓名待確認）",
  "質詢對象": "衛生局長 | 聯醫院長 | 兩者 | 不明",
  "質詢主題": "15字以內標題",
  "質詢摘要": "100-200字，議員提出了什麼問題或要求",
  "局方回應": "局長或院長的回應重點（若無則填空字串）",
  "承諾事項": ["具體承諾1", "具體承諾2"],
  "待辦追蹤": ["需後續處理的事項"],
  "緊急程度": "立即 | 本週 | 本月 | 追蹤",
  "相關單位": ["聯醫-仁愛", "衛生局政策"],
  "關鍵數字": {{"項目名稱": "數值與單位"}},
  "情緒張力": "平和 | 追問 | 激烈"
}}"""


# ------------------------------------------------------------------
# Summarizer
# ------------------------------------------------------------------

class Summarizer:
    def __init__(self, host: str, model: str, max_retries: int = 3):
        self._client = Client(host=host)
        self._model = model
        self._max_retries = max_retries
        logger.info(f"Summarizer 初始化｜模型：{model}｜host：{host}")

    def generate(
        self,
        transcript: str,
        timestamp: str | None = None,
    ) -> SessionSummary | None:
        """
        生成結構化摘要。
        回傳 SessionSummary，或 None（超過重試次數）。
        """
        if not timestamp:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

        prompt = _USER_TMPL.format(transcript=transcript, timestamp=timestamp)

        for attempt in range(1, self._max_retries + 1):
            try:
                raw = self._call_llm(prompt)
                summary = self._parse_and_validate(raw)
                logger.info(
                    f"摘要生成成功｜主題：{summary.質詢主題}"
                    f"｜緊急：{summary.緊急程度}"
                )
                return summary
            except Exception as e:
                logger.warning(f"摘要第 {attempt} 次失敗：{e}")

        logger.error("摘要生成失敗，已達最大重試次數")
        return None

    # ------------------------------------------------------------------
    # 內部
    # ------------------------------------------------------------------

    def _call_llm(self, user_prompt: str) -> str:
        response = self._client.chat(
            model=self._model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            options={
                "temperature": 0.1,   # 結構化輸出用低溫
                "num_predict": 1024,
                "num_ctx": 8192,      # 逐字稿約 3000–6000 tokens
            },
        )
        return response["message"]["content"]

    def _parse_and_validate(self, raw: str) -> SessionSummary:
        # LLM 有時在 JSON 前後加說明文字，取第一個完整 JSON 物件
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError(f"回應中找不到 JSON：{raw[:100]}")
        data = json.loads(match.group())
        return SessionSummary(**data)
