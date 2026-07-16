from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class Permission(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    UNRESTRICTED = "unrestricted"

    @property
    def rank(self) -> int:
        return {
            Permission.READ_ONLY: 0,
            Permission.WORKSPACE_WRITE: 1,
            Permission.UNRESTRICTED: 2,
        }[self]


class TaskState(StrEnum):
    SUBMITTED = "submitted"
    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

    @property
    def terminal(self) -> bool:
        return self in {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.TIMED_OUT,
            TaskState.CANCELLED,
            TaskState.REJECTED,
        }


@dataclass(slots=True)
class TaskRequest:
    agent: str
    prompt: str
    cwd: Path
    permission: Permission = Permission.READ_ONLY
    timeout_seconds: float = 1800.0
    files: list[Path] = field(default_factory=list)
    images: list[Path] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "prompt": self.prompt,
            "cwd": str(self.cwd),
            "permission": self.permission.value,
            "timeout_seconds": self.timeout_seconds,
            "files": [str(path) for path in self.files],
            "images": [str(path) for path in self.images],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TaskRequest:
        return cls(
            agent=str(value["agent"]),
            prompt=str(value["prompt"]),
            cwd=Path(value["cwd"]),
            permission=Permission(value["permission"]),
            timeout_seconds=float(value["timeout_seconds"]),
            files=[Path(path) for path in value.get("files", [])],
            images=[Path(path) for path in value.get("images", [])],
        )


@dataclass(slots=True)
class TaskError:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)
