"""运行状态、执行锁与进度日志（journal）。

每个草稿对应一个 journal 文件，记录：
- 材料编号 -> 当前 upload_id、本地摘要、状态沿革
- 已被取代的旧 upload_id（迟到的预检结果不得复活它们）
- 草稿核对结果与输出路径

一个草稿同时只允许一个活动执行者：通过 fcntl 排它锁保证，
锁随进程生命周期持有，取消运行后 journal 保留（草稿已上传部分不丢失）。
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import LockError

# 材料状态机
STATE_PENDING = "待上传"
STATE_UPLOADED = "已上传待预检"
STATE_PASSED = "预检通过已关联"
STATE_FAILED = "预检失败"
STATE_REPLACED = "已替换待重传"
STATE_SKIPPED_EXISTING = "恢复确认已存在跳过"
STATE_NEEDS_HUMAN = "需要人工处理"

TERMINAL_STATES = {
    STATE_PASSED,
    STATE_FAILED,
    STATE_SKIPPED_EXISTING,
    STATE_NEEDS_HUMAN,
}


@dataclass
class Attempt:
    """一次具体的文件选择记录；同材料每次替换都产生新记录，编号沿革保留。"""

    upload_id: str
    digest: str
    path: str
    state: str
    superseded: bool = False


@dataclass
class MaterialState:
    material_id: str
    project: str
    kind: str
    summary: str
    digest: str
    path: str
    dynamic_fields: dict[str, str]
    # 当前生效的 upload_id（可能为 None）
    current_upload_id: str | None = None
    state: str = STATE_PENDING
    attempts: list[Attempt] = field(default_factory=list)
    note: str = ""

    def live_attempts(self) -> list[Attempt]:
        return [a for a in self.attempts if not a.superseded]

    def superseded_upload_ids(self) -> set[str]:
        return {a.upload_id for a in self.attempts if a.superseded}


@dataclass
class Journal:
    draft_id: str
    manifest_path: str
    portal_url: str
    materials: dict[str, MaterialState]
    verified_draft_id: str | None = None
    preview_path: str | None = ""
    preview_url: str | None = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Journal":
        materials = {
            mid: MaterialState(
                **{
                    **item,
                    "attempts": [Attempt(**attempt) for attempt in item.get("attempts", [])],
                }
            )
            for mid, item in data.get("materials", {}).items()
        }
        return cls(
            draft_id=data["draft_id"],
            manifest_path=data["manifest_path"],
            portal_url=data["portal_url"],
            materials=materials,
            verified_draft_id=data.get("verified_draft_id"),
            preview_path=data.get("preview_path") or "",
            preview_url=data.get("preview_url") or "",
        )


class JournalStore:
    """负责 journal 文件的原子读写与排它执行锁。"""

    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock_handle = None
        self._lock_path: Path | None = None

    def journal_path(self, draft_id: str) -> Path:
        safe = draft_id.replace(os.sep, "_")
        return self.state_dir / f"journal-{safe}.json"

    def acquire_lock(self, draft_id: str) -> None:
        """获取草稿级排它锁；已有活动执行者时抛 LockError。"""
        self._lock_path = self.state_dir / f"lock-{draft_id.replace(os.sep, '_')}.lock"
        handle = open(self._lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise LockError(
                f"草稿 {draft_id} 已有活动执行者持有锁；"
                "如需在本进程恢复，请先停止原运行或改用 resume",
                items=[draft_id],
            )
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        self._lock_handle = handle

    def release_lock(self) -> None:
        if self._lock_handle is None:
            return
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_handle.close()
            self._lock_handle = None

    def load(self, draft_id: str) -> Journal | None:
        path = self.journal_path(draft_id)
        if not path.is_file():
            return None
        return Journal.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, journal: Journal) -> None:
        """原子替换写盘，保证中断后 journal 不半截。"""
        target = self.journal_path(journal.draft_id)
        fd, tmp_name = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=self.state_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(journal.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
