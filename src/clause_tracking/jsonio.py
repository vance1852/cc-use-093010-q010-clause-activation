"""确定性的 JSON 与内容摘要工具。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def canonical_json(value: object) -> str:
    """生成跨平台一致的紧凑 JSON 文本。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_digest(values: Iterable[object]) -> str:
    """按输入顺序计算规范化内容摘要。"""

    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value.strip()) != 64:
        raise ValueError(f"{field} 必须是 64 位 SHA-256")
    try:
        int(value.strip(), 16)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是十六进制摘要") from exc
    return value.strip().lower()
