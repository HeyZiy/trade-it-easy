"""投资账户建议使用的短哈希标识。"""

import hashlib


def short_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]
