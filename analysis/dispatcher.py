"""
P3 - 單位分派

兩層決策：
  Layer 1：規則引擎（dispatch_rules.py 對照表）→ 高信心直接輸出
  Layer 2：Gemma 1B LLM 補充判斷（跨院業務、模糊情境）

信心門檻 HIGH_CONF_THRESHOLD：任一單位得分超過此值 → 直接輸出，不送 LLM
"""
import json
import re
from dataclasses import dataclass, field

from ollama import Client
from loguru import logger

from analysis.summarizer import SessionSummary
from data.dispatch_rules import UNIT_RULES

HIGH_CONF_THRESHOLD = 1.5   # 規則引擎累積分超過此值視為高信心


# ------------------------------------------------------------------
# 資料類別
# ------------------------------------------------------------------

@dataclass
class DispatchResult:
    primary: list[str]          # 主責單位
    secondary: list[str]        # 協辦單位
    source: str                 # "規則引擎" | "LLM" | "混合"
    confidence: float           # 0–1
    matched_terms: list[str] = field(default_factory=list)


# ------------------------------------------------------------------
# Dispatcher
# ------------------------------------------------------------------

class Dispatcher:
    def __init__(self, host: str, model: str):
        self._client = Client(host=host)
        self._model = model
        logger.info(f"Dispatcher 初始化｜模型：{model}")

    def dispatch(self, summary: SessionSummary) -> DispatchResult:
        """
        輸入 SessionSummary，回傳 DispatchResult。
        規則引擎高信心 → 直接輸出；否則送 Gemma 1B 判斷。
        """
        rule_scores = self._rule_engine(summary)

        # 找出高信心命中單位
        high_conf = {
            u: v for u, v in rule_scores.items()
            if v["score"] >= HIGH_CONF_THRESHOLD
        }

        if high_conf:
            # 分數最高為主責，其餘為協辦
            sorted_units = sorted(
                high_conf, key=lambda u: high_conf[u]["score"], reverse=True
            )
            primary = [sorted_units[0]]
            secondary = sorted_units[1:]
            conf = min(high_conf[sorted_units[0]]["score"] / 3.0, 1.0)
            all_terms = [
                t for u in sorted_units for t in high_conf[u]["matched"]
            ]
            logger.info(
                f"規則引擎高信心｜主責：{primary}｜命中：{all_terms}"
            )
            return DispatchResult(
                primary=primary,
                secondary=secondary,
                source="規則引擎",
                confidence=round(conf, 2),
                matched_terms=all_terms,
            )

        # 低信心或無命中 → LLM
        logger.info("規則引擎低信心，送 Gemma 1B 判斷")
        return self._llm_dispatch(summary, rule_scores)

    # ------------------------------------------------------------------
    # 規則引擎
    # ------------------------------------------------------------------

    def _rule_engine(self, summary: SessionSummary) -> dict[str, dict]:
        """
        對照 UNIT_RULES 計算各單位得分。
        回傳 {unit: {"score": float, "matched": [term, ...]}}
        """
        # 合併所有文字欄位
        searchable = " ".join([
            summary.質詢摘要,
            summary.局方回應,
            " ".join(summary.承諾事項),
            " ".join(summary.待辦追蹤),
            " ".join(summary.相關單位),
        ])

        scores: dict[str, dict] = {}
        for unit, rules in UNIT_RULES.items():
            score = 0.0
            matched: list[str] = []

            for term in rules["地名"]:
                if term in searchable:
                    score += 0.9 * rules["weight"]
                    matched.append(f"地名:{term}")

            for term in rules["科別"]:
                if term in searchable:
                    score += 0.7 * rules["weight"]
                    matched.append(f"科別:{term}")

            for term in rules["業務"]:
                if term in searchable:
                    score += 0.5 * rules["weight"]
                    matched.append(f"業務:{term}")

            if score > 0:
                scores[unit] = {"score": round(score, 2), "matched": matched}

        if scores:
            top = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)
            logger.debug(
                f"規則引擎前三：{[(u, v['score']) for u, v in top[:3]]}"
            )
        else:
            logger.debug("規則引擎無命中")

        return scores

    # ------------------------------------------------------------------
    # LLM 分派
    # ------------------------------------------------------------------

    def _llm_dispatch(
        self,
        summary: SessionSummary,
        rule_scores: dict,
    ) -> DispatchResult:
        # 把規則引擎初步結果帶進 prompt，給 LLM 參考
        rule_hint = ""
        if rule_scores:
            top = sorted(
                rule_scores.items(), key=lambda x: x[1]["score"], reverse=True
            )[:3]
            rule_hint = (
                "\n\n規則引擎初步命中（供參考）：\n"
                + "\n".join(f"  {u}（分數 {v['score']}）：{v['matched']}" for u, v in top)
            )

        prompt = f"""質詢主題：{summary.質詢主題}
質詢摘要：{summary.質詢摘要}
承諾事項：{'; '.join(summary.承諾事項)}
待辦追蹤：{'; '.join(summary.待辦追蹤)}
{rule_hint}

各院區業務簡介：
- 聯醫-仁愛：大安信義，急診、骨科、心臟科
- 聯醫-中興：萬華大同，慢性病、弱勢醫療、結核防治
- 聯醫-忠孝：南港內湖，腎臟科、洗腎
- 聯醫-松德：信義，精神科、失智症、自殺防治
- 聯醫-陽明：士林北投，老年醫學、長照、安寧
- 聯醫-婦幼：中山，婦產科、兒科
- 聯醫-和平：中正，感染科、傳染病
- 衛生局政策：跨院政策、預算、人力、整體品質

請判斷應分派給哪些單位。只回覆 JSON：
{{
  "主責單位": ["單位名稱"],
  "協辦單位": ["單位名稱"],
  "判斷理由": "一句話",
  "信心": 0.0
}}"""

        try:
            response = self._client.chat(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.0, "num_predict": 256},
            )
            raw = response["message"]["content"]
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not match:
                raise ValueError("LLM 回應無 JSON")

            data = json.loads(match.group())
            primary = data.get("主責單位", [])
            secondary = data.get("協辦單位", [])
            conf = float(data.get("信心", 0.5))
            reason = data.get("判斷理由", "")

            logger.info(
                f"LLM 分派｜主責：{primary}｜協辦：{secondary}"
                f"｜信心：{conf:.2f}｜理由：{reason}"
            )
            return DispatchResult(
                primary=primary,
                secondary=secondary,
                source="LLM",
                confidence=round(conf, 2),
            )

        except Exception as e:
            logger.error(f"LLM 分派失敗：{e}，fallback 到衛生局政策")
            return DispatchResult(
                primary=["衛生局政策"],
                secondary=[],
                source="fallback",
                confidence=0.3,
            )
