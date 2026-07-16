from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CLIExecError(Exception):
    code: str
    message: str
    details: dict[str, object] | None = None
    exit_code: int = 2

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details:
            value["details"] = self.details
        return value


CONFIG_ERROR = "CONFIG_ERROR"
AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
RUN_NOT_FOUND = "RUN_NOT_FOUND"
RESULT_NOT_READY = "RESULT_NOT_READY"
UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
PERMISSION_DENIED = "PERMISSION_DENIED"
CONCURRENCY_LIMIT = "CONCURRENCY_LIMIT"
WORKSPACE_BUSY = "WORKSPACE_BUSY"
NESTED_DELEGATION = "NESTED_DELEGATION"
SPAWN_ERROR = "SPAWN_ERROR"
NONZERO_EXIT = "NONZERO_EXIT"
PROTOCOL_ERROR = "PROTOCOL_ERROR"
TIMEOUT = "TIMEOUT"
CANCELLED = "CANCELLED"
OUTPUT_LIMIT = "OUTPUT_LIMIT"
SUPERVISOR_LOST = "SUPERVISOR_LOST"
