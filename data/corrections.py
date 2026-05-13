# Whisper 常見誤辨修正詞表（依實際使用後持續補充）
CORRECTIONS: dict[str, list[str]] = {
    "衛生局長": ["衛生骨長", "衛生古長", "位生局長", "衛生居長"],
    "聯醫院長": ["聯醫延長", "聯美院長", "聯一院長"],
    "台北市立聯合醫院": ["台北市立聯合醫源", "台北市立聯一醫院"],
    "台北市議會": ["台北市議卡", "台北市議化"],
    "衛生局": ["衛生骨", "衛生谷"],
    "質詢": ["質訊", "質循"],
}


def post_correct(text: str) -> str:
    for correct, wrongs in CORRECTIONS.items():
        for wrong in wrongs:
            text = text.replace(wrong, correct)
    return text
