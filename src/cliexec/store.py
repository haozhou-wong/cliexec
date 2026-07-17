from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import RESULT_NOT_READY, RUN_NOT_FOUND, CLIExecError
from .models import SCHEMA_VERSION, TaskState
from .util import permission_mode, state_home, utc_now

ACTIVE_STATES = {
    TaskState.SUBMITTED.value,
    TaskState.STARTING.value,
    TaskState.RUNNING.value,
}

_INTERNAL_STATE_FIELDS = {
    "native_session_id",
    "session_claimed",
    "continued_by_run_id",
}


class RunStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or state_home()).expanduser().resolve()
        self.runs_dir = self.root / "runs"
        self.locks_dir = self.root / "locks"
        self._ensure_dir(self.root)
        self._ensure_dir(self.runs_dir)
        self._ensure_dir(self.locks_dir)

    @staticmethod
    def _ensure_dir(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        permission_mode(path, 0o700)

    @contextmanager
    def registry_lock(self) -> Iterator[None]:
        path = self.locks_dir / "registry.lock"
        with path.open("a+b") as handle:
            permission_mode(path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def run_lock(self, run_id: str) -> Iterator[None]:
        path = self.run_dir(run_id) / "run.lock"
        with path.open("a+b") as handle:
            permission_mode(path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def run_dir(self, run_id: str) -> Path:
        if not run_id or any(char not in "0123456789abcdef-" for char in run_id.lower()):
            raise CLIExecError(RUN_NOT_FOUND, f"invalid run id: {run_id}")
        path = self.runs_dir / run_id
        if not path.is_dir():
            raise CLIExecError(RUN_NOT_FOUND, f"run not found: {run_id}")
        return path

    def paths(self, run_id: str) -> dict[str, Path]:
        directory = self.run_dir(run_id)
        return {
            "directory": directory,
            "request": directory / "request.json",
            "config": directory / "config.toml",
            "state": directory / "state.json",
            "events": directory / "events.jsonl",
            "stdout": directory / "stdout.log",
            "stderr": directory / "stderr.log",
            "result": directory / "result.txt",
            "result_meta": directory / "result.json",
        }

    @staticmethod
    def _atomic_bytes(path: Path, payload: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            permission_mode(path, 0o600)
        finally:
            temporary_path.unlink(missing_ok=True)

    @classmethod
    def _atomic_json(cls, path: Path, value: dict[str, Any]) -> None:
        payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        cls._atomic_bytes(path, payload)

    def create_run(
        self,
        *,
        request: dict[str, Any],
        agent: dict[str, Any],
        policy: dict[str, Any],
        config_snapshot: str,
        conversation_id: str | None = None,
        parent_run_id: str | None = None,
        native_session_id: str | None = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        directory = self.runs_dir / run_id
        directory.mkdir(mode=0o700)
        permission_mode(directory, 0o700)
        paths = {
            "request": directory / "request.json",
            "config": directory / "config.toml",
            "state": directory / "state.json",
            "events": directory / "events.jsonl",
        }
        payload = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "request": request,
            "agent_config": agent,
            "policy": policy,
        }
        self._atomic_json(paths["request"], payload)
        self._atomic_bytes(paths["config"], config_snapshot.encode())
        created_at = utc_now()
        state = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "agent": request["agent"],
            "conversation_id": conversation_id,
            "parent_run_id": parent_run_id,
            "native_session_id": native_session_id,
            "session_claimed": False,
            "continued_by_run_id": None,
            "state": TaskState.SUBMITTED.value,
            "permission": {
                "requested": request["permission"],
                "effective": request["permission"],
            },
            "cwd": request["cwd"],
            "created_at": created_at,
            "updated_at": created_at,
            "started_at": None,
            "finished_at": None,
            "last_activity_at": created_at,
            "supervisor_pid": None,
            "supervisor_start_ticks": None,
            "child_pid": None,
            "child_pgid": None,
            "child_start_ticks": None,
            "exit_code": None,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "cancel_requested": False,
            "error": None,
        }
        self._atomic_json(paths["state"], state)
        self._atomic_bytes(
            paths["events"],
            (json.dumps({"at": created_at, "state": TaskState.SUBMITTED.value}) + "\n").encode(),
        )
        return run_id

    def load_request(self, run_id: str) -> dict[str, Any]:
        path = self.paths(run_id)["request"]
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CLIExecError(RUN_NOT_FOUND, f"cannot read request for {run_id}: {exc}") from exc

    def load_state(self, run_id: str) -> dict[str, Any]:
        path = self.paths(run_id)["state"]
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CLIExecError(RUN_NOT_FOUND, f"cannot read state for {run_id}: {exc}") from exc

    def update_state(self, run_id: str, **changes: Any) -> dict[str, Any]:
        paths = self.paths(run_id)
        with self.run_lock(run_id):
            try:
                state = json.loads(paths["state"].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CLIExecError(RUN_NOT_FOUND, f"cannot update run {run_id}: {exc}") from exc
            previous = state.get("state")
            state.update(changes)
            state["updated_at"] = utc_now()
            self._atomic_json(paths["state"], state)
            if state.get("state") != previous:
                event = {
                    "at": state["updated_at"],
                    "state": state["state"],
                    "error": state.get("error"),
                }
                with paths["events"].open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                permission_mode(paths["events"], 0o600)
            return state

    def request_cancel(self, run_id: str) -> dict[str, Any]:
        state = self.load_state(run_id)
        if TaskState(state["state"]).terminal:
            return state
        return self.update_state(run_id, cancel_requested=True)

    def list_states(self) -> list[dict[str, Any]]:
        states: list[dict[str, Any]] = []
        for directory in self.runs_dir.iterdir():
            if not directory.is_dir():
                continue
            try:
                states.append(self.load_state(directory.name))
            except CLIExecError:
                continue
        states.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return states

    @staticmethod
    def public_state(state: dict[str, Any]) -> dict[str, Any]:
        value = dict(state)
        value.setdefault("conversation_id", None)
        value.setdefault("parent_run_id", None)
        task_state = TaskState(value["state"])
        value["resumable"] = bool(
            value.get("conversation_id")
            and value.get("native_session_id")
            and value.get("session_claimed")
            and task_state.terminal
            and task_state is not TaskState.REJECTED
            and not value.get("continued_by_run_id")
        )
        for field in _INTERNAL_STATE_FIELDS:
            value.pop(field, None)
        return value

    def release_unclaimed_continuation(self, run_id: str, *, registry_locked: bool = False) -> None:
        def release() -> None:
            child = self.load_state(run_id)
            parent_run_id = child.get("parent_run_id")
            if not isinstance(parent_run_id, str) or child.get("session_claimed"):
                return
            try:
                parent = self.load_state(parent_run_id)
            except CLIExecError:
                return
            if parent.get("continued_by_run_id") == run_id:
                self.update_state(parent_run_id, continued_by_run_id=None)

        if registry_locked:
            release()
            return
        with self.registry_lock():
            release()

    def save_result(self, run_id: str, text: str, metadata: dict[str, Any]) -> None:
        paths = self.paths(run_id)
        self._atomic_bytes(paths["result"], text.encode("utf-8"))
        self._atomic_json(paths["result_meta"], metadata)

    @staticmethod
    def _inline(path: Path, limit: int) -> tuple[str | None, bool]:
        if not path.exists():
            return None, False
        size = path.stat().st_size
        if size <= limit:
            return path.read_text(encoding="utf-8", errors="replace"), False
        marker = b"\n\n[... CLIExec inline result truncated ...]\n\n"
        available = max(0, limit - len(marker))
        head_size = available // 2
        tail_size = available - head_size
        with path.open("rb") as handle:
            head = handle.read(head_size)
            handle.seek(-tail_size, os.SEEK_END)
            tail = handle.read(tail_size)
        return (head + marker + tail).decode("utf-8", errors="replace"), True

    def result_view(self, run_id: str, inline_limit: int) -> dict[str, Any]:
        state = self.load_state(run_id)
        task_state = TaskState(state["state"])
        if not task_state.terminal:
            raise CLIExecError(
                RESULT_NOT_READY,
                f"run {run_id} is still {task_state.value}",
                exit_code=3,
            )
        paths = self.paths(run_id)
        text, truncated = self._inline(paths["result"], inline_limit)
        completed = task_state is TaskState.COMPLETED
        result = {
            **self.public_state(state),
            "succeeded": completed,
            "duration_ms": _duration_ms(state.get("started_at"), state.get("finished_at")),
            "final_text": text if completed else None,
            "partial_text": text if not completed else None,
            "truncated": truncated,
            "untrusted": True,
            "usage": None,
            "files": {
                "stdout": str(paths["stdout"]),
                "stderr": str(paths["stderr"]),
                "result": str(paths["result"]),
            },
        }
        for internal in (
            "updated_at",
            "supervisor_start_ticks",
            "child_start_ticks",
            "cancel_requested",
        ):
            result.pop(internal, None)
        return result

    def purge(self, retention_days: int, *, force: bool = False) -> list[str]:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        removed: list[str] = []
        with self.registry_lock():
            for state in self.list_states():
                if state.get("state") in ACTIVE_STATES:
                    continue
                try:
                    created = datetime.fromisoformat(
                        str(state["created_at"]).replace("Z", "+00:00")
                    )
                except (KeyError, ValueError):
                    continue
                if force or created < cutoff:
                    shutil.rmtree(self.runs_dir / state["run_id"], ignore_errors=True)
                    removed.append(state["run_id"])
        return removed

    def maybe_purge(self, retention_days: int) -> None:
        marker = self.root / ".last-purge"
        try:
            if marker.exists() and time.time() - marker.stat().st_mtime < 86400:
                return
            self.purge(retention_days)
            self._atomic_bytes(marker, utc_now().encode())
        except OSError:
            return


def _duration_ms(started: object, finished: object) -> int | None:
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    try:
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def proc_start_ticks(pid: int) -> int | None:
    try:
        content = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        remainder = content[content.rfind(")") + 2 :].split()
        return int(remainder[19])
    except (OSError, ValueError, IndexError):
        return None


def same_process(pid: object, expected_ticks: object) -> bool:
    if not isinstance(pid, int) or not isinstance(expected_ticks, int):
        return False
    return proc_start_ticks(pid) == expected_ticks
