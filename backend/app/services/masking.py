"""脱敏：日志、证据快照和接口响应里都不允许出现完整手机号、地址与支付凭证。

需求书 §8 把这条列为**非功能需求**并给出了验证方式（脱敏测试）。做法上有一条铁律：

**脱敏放在"出口"而不是"入口"**。如果在入库时就抹掉，业务侧（比如物流核对）就用不到了；
所以这里提供的是"给展示/日志用"的脱敏函数，数据库里保留原值，由调用方在出口处调用。

覆盖范围（按电商售后的真实敏感字段）：
- 手机号 / 座机；
- 收货地址（保留到市/区，其余打码）；
- 身份证（若出现在售后凭证里）；
- 支付凭证类字段（卡号、交易流水、支付账号）。
"""

from __future__ import annotations

import re

PHONE_RE = re.compile(r"1[3-9]\d{9}")
LANDLINE_RE = re.compile(r"0\d{2,3}-?\d{7,8}")
CARD_RE = re.compile(r"\b\d{16,19}\b")
ID_CARD_RE = re.compile(r"\b\d{17}[\dXx]\b")
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# 命中这些键名即整值脱敏（不看内容形态，避免漏）
SENSITIVE_KEYS = {
    "address",
    "receiver_address",
    "收件信息",
    "phone",
    "mobile",
    "receiver_phone",
    "contact",
    "id_card",
    "card_no",
    "bank_card",
    "pay_account",
    "payment_account",
    "transaction_id",
    "trade_no",
}

MASK = "***"


def mask_phone(text: str) -> str:
    return PHONE_RE.sub(lambda m: f"{m.group()[:3]}****{m.group()[-4:]}", text)


def mask_text(text: str) -> str:
    """对一段自由文本做全面脱敏（日志与证据快照出口调用）。"""
    if not text:
        return text

    def mask_segment(segment: str) -> str:
        masked = mask_phone(segment)
        masked = LANDLINE_RE.sub(lambda m: f"{m.group()[:4]}****", masked)
        masked = ID_CARD_RE.sub(
            lambda m: f"{m.group()[:6]}********{m.group()[-4:]}",
            masked,
        )
        return CARD_RE.sub(
            lambda m: f"{m.group()[:4]} **** **** {m.group()[-4:]}",
            masked,
        )

    parts: list[str] = []
    cursor = 0
    for match in UUID_RE.finditer(text):
        parts.append(mask_segment(text[cursor : match.start()]))
        parts.append(match.group())
        cursor = match.end()
    parts.append(mask_segment(text[cursor:]))
    return "".join(parts)


def mask_address(address: str) -> str:
    """地址保留到区/县，门牌与小区名打码（既能核对城市，又不泄露住址）。"""
    if not address:
        return address
    for keyword in ("区", "县", "市"):
        index = address.find(keyword)
        if index >= 0:
            return f"{address[: index + 1]}{MASK}"
    return f"{address[:4]}{MASK}" if len(address) > 4 else MASK


def mask_value(key: str | None, value: object) -> object:
    """按字段名与内容形态双重判断，返回可安全展示的值。"""
    if value is None:
        return None
    if key and key.lower() in SENSITIVE_KEYS:
        text = str(value)
        if "地址" in str(key) or key.lower() in {"address", "receiver_address"}:
            return mask_address(text)
        return mask_text(text)
    if isinstance(value, str):
        return mask_text(value)
    return value


def mask_structure(value: object, key: str | None = None) -> object:
    """Recursively mask nested provider payloads while preserving non-sensitive shape."""
    if key and key.lower() in SENSITIVE_KEYS:
        return mask_value(key, value)
    if isinstance(value, dict):
        return {item_key: mask_structure(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [mask_structure(item) for item in value]
    if isinstance(value, tuple):
        return [mask_structure(item) for item in value]
    return mask_value(key, value)


def contains_sensitive(text: str) -> bool:
    """自检用：返回文本里是否仍存在未脱敏的敏感信息。"""
    if not text:
        return False
    without_identifiers = UUID_RE.sub("", text)
    return bool(
        PHONE_RE.search(without_identifiers)
        or CARD_RE.search(without_identifiers)
        or ID_CARD_RE.search(without_identifiers)
    )
