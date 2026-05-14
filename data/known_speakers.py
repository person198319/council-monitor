"""
已知說話者資料庫
優先識別對象：王智弘院長（聯醫）、黃建華局長（衛生局）
priority 數字越小越優先識別
"""
from dataclasses import dataclass, field


@dataclass
class KnownSpeaker:
    name: str
    aliases: list[str]      # 別人稱呼他的方式（用於偵測他被點名）
    self_ref: list[str]     # 他自己說話時常用的自我指涉詞
    role: str               # 顯示用職稱
    priority: int           # 識別優先序


KNOWN_SPEAKERS: list[KnownSpeaker] = [
    KnownSpeaker(
        name="王智弘院長",
        aliases=[
            "王院長", "王智弘", "王智弘院長",
            "聯醫院長", "聯合醫院院長", "總院長",
        ],
        self_ref=[
            "本院", "我們聯醫", "我們聯合醫院",
            "本總院", "各院區", "聯醫",
        ],
        role="聯醫院長",
        priority=1,
    ),
    KnownSpeaker(
        name="黃建華局長",
        aliases=[
            "黃局長", "黃建華", "黃建華局長",
            "衛生局長", "局長",
        ],
        self_ref=[
            "本局", "我們衛生局", "衛生局",
            "本府衛生局",
        ],
        role="衛生局長",
        priority=2,
    ),
]

# 別名 → KnownSpeaker 快速查找
ALIAS_TO_SPEAKER: dict[str, KnownSpeaker] = {
    alias: spk
    for spk in KNOWN_SPEAKERS
    for alias in spk.aliases
}
