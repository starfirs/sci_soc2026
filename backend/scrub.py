"""本地脱敏：把姓名、手机号、邮箱、身份证替换为稳定占位符。

里程碑 1 只做正则 + 已知姓名；设计文档中的 spaCy 实体识别（地点、机构）
留给 Task 2 的完整实现。原始值只写入本机 placeholder_map 表。
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping

# 顺序很重要：身份证、银行卡比手机号更长，必须先匹配
_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_BANK_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

KIND_LABELS = {
    "person": "人物",
    "phone": "手机",
    "email": "邮箱",
    "id_card": "身份证",
}


def _next_placeholder(kind: str, taken: set[str]) -> str:
    n = 1
    while f"[{KIND_LABELS[kind]}{n}]" in taken:
        n += 1
    return f"[{KIND_LABELS[kind]}{n}]"


def mask(
    text: str,
    *,
    known_names: Iterable[str] = (),
    existing: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """返回 ``(脱敏文本, {占位符: 真实值})``。

    ``existing`` 传入数据库中已有的 占位符→真实值 映射，保证同一实体
    始终映射到同一个占位符。返回值只包含本次新出现的映射。
    """
    existing = dict(existing or {})
    real_to_placeholder = {real: ph for ph, real in existing.items()}
    found: dict[str, str] = {}
    taken = set(existing)

    def placeholder(kind: str, real: str) -> str:
        if real in real_to_placeholder:
            return real_to_placeholder[real]
        ph = _next_placeholder(kind, taken)
        real_to_placeholder[real] = ph
        taken.add(ph)
        found[ph] = real
        return ph

    # 长名优先，避免“张三”先替换掉“张三丰”的一部分
    for name in sorted({n for n in known_names if n}, key=len, reverse=True):
        text = text.replace(name, placeholder("person", name))

    text = _ID_RE.sub(lambda m: placeholder("id_card", m.group()), text)
    text = _EMAIL_RE.sub(lambda m: placeholder("email", m.group()), text)
    # 设计文档：账号类高敏感内容直接丢弃，不留占位符
    text = _BANK_RE.sub("", text)
    text = _PHONE_RE.sub(lambda m: placeholder("phone", m.group()), text)

    return text, found
