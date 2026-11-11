"""本地文件摘要工具。

门户用浏览器 crypto.subtle 计算 SHA-256，RPA 侧用同一算法预先计算，
使同名校名异内容的文件可以凭摘要而非文件名认领附件。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024


def sha256_file(path: str | Path) -> str:
    """返回文件的 SHA-256 十六进制摘要，与门户 crypto.subtle.digest 一致。"""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def is_empty_file(path: str | Path) -> bool:
    return Path(path).stat().st_size == 0
