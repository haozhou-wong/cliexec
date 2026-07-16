from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AgentConfig, AppConfig, PolicyConfig, load_config, toml_dumps
from .errors import (
    CONCURRENCY_LIMIT,
    NESTED_DELEGATION,
    PERMISSION_DENIED,
    SPAWN_ERROR,
    SUPERVISOR_LOST,
    UNSUPPORTED_CAPABILITY,
    WORKSPACE_BUSY,
    CLIExecError,
)
from .models import Permission, TaskRequest, TaskState
from .store import ACTIVE_STATES, RunStore, proc_start_ticks, same_process
from .util import utc_now


def _policy_dict(policy: PolicyConfig) -> dict[str, Any]:
    return {
        "max_concurrency": policy.max_concurrency,
        "default_timeout": policy.default_timeout,
        "max_timeout": policy.max_timeout,
        "max_permission": policy.max_permission.value,
        "retention_days": policy.retention_days,
        "inline_result_bytes": policy.inline_result_bytes,
        "max_output_bytes": policy.max_output_bytes,
    }


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _kill_group(pgid: object) -> None:
    if not isinstance(pgid, int):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    with suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


def _kill_recorded_child(state: dict[str, Any]) -> None:
    child_pid = state.get("child_pid")
    child_pgid = state.get("child_pgid")
    if child_pid != child_pgid:
        return
    if same_process(child_pid, state.get("child_start_ticks")):
        _kill_group(child_pgid)


class TaskService:
    def __init__(
        self,
        *,
        config: AppConfig | None = None,
        config_path: Path | None = None,
        store: RunStore | None = None,
    ) -> None:
        self.config = config or load_config(config_path)
        self.store = store or RunStore()
        self.store.maybe_purge(self.config.policy.retention_days)

    def normalize_request(self, request: TaskRequest) -> TaskRequest:
        try:
            depth = int(os.environ.get("CLIEXEC_DEPTH", "0"))
        except ValueError:
            depth = 1
        if depth >= 1:
            raise CLIExecError(NESTED_DELEGATION, "nested CLIExec delegation is disabled")
        cwd = request.cwd.expanduser().resolve()
        if not cwd.is_dir():
            raise CLIExecError("INVALID_REQUEST", f"cwd is not a directory: {cwd}")
        prompt = request.prompt
        if not prompt.strip():
            raise CLIExecError("INVALID_REQUEST", "prompt cannot be empty")
        files = [self._resolve_input_path(path, cwd, "file") for path in request.files]
        images = [self._resolve_input_path(path, cwd, "image") for path in request.images]
        if request.timeout_seconds <= 0:
            raise CLIExecError("INVALID_REQUEST", "timeout must be positive")
        if request.timeout_seconds > self.config.policy.max_timeout:
            raise CLIExecError(
                "INVALID_REQUEST",
                f"timeout exceeds policy maximum of {self.config.policy.max_timeout:g} seconds",
            )
        return TaskRequest(
            agent=request.agent,
            prompt=prompt,
            cwd=cwd,
            permission=request.permission,
            timeout_seconds=request.timeout_seconds,
            files=files,
            images=images,
        )

    @staticmethod
    def _resolve_input_path(path: Path, cwd: Path, kind: str) -> Path:
        resolved = path.expanduser()
        if not resolved.is_absolute():
            resolved = cwd / resolved
        resolved = resolved.resolve()
        if not resolved.is_file():
            raise CLIExecError("INVALID_REQUEST", f"{kind} does not exist: {resolved}")
        return resolved

    def _new_run(self, request: TaskRequest, agent: AgentConfig) -> str:
        return self.store.create_run(
            request=request.to_dict(),
            agent=agent.to_dict(),
            policy=_policy_dict(self.config.policy),
            config_snapshot=toml_dumps(self.config.raw),
        )

    def _reject(
        self, request: TaskRequest, agent: AgentConfig, code: str, message: str
    ) -> dict[str, Any]:
        run_id = self._new_run(request, agent)
        self.store.save_result(
            run_id,
            "",
            {"state": TaskState.REJECTED.value, "error": {"code": code, "message": message}},
        )
        return self.store.update_state(
            run_id,
            state=TaskState.REJECTED.value,
            finished_at=utc_now(),
            error={"code": code, "message": message},
        )

    def _reconcile(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("state") not in ACTIVE_STATES:
            return state
        pid = state.get("supervisor_pid")
        ticks = state.get("supervisor_start_ticks")
        if same_process(pid, ticks):
            return state
        if state.get("state") in {TaskState.SUBMITTED.value, TaskState.STARTING.value}:
            try:
                created = datetime.fromisoformat(str(state["created_at"]).replace("Z", "+00:00"))
                if (datetime.now(created.tzinfo) - created).total_seconds() < 10:
                    return state
            except (KeyError, ValueError):
                pass
        _kill_recorded_child(state)
        run_id = state["run_id"]
        paths = self.store.paths(run_id)
        partial = ""
        if paths["stdout"].exists():
            partial = paths["stdout"].read_text(encoding="utf-8", errors="replace")[-65536:]
        self.store.save_result(
            run_id,
            partial,
            {
                "state": TaskState.FAILED.value,
                "error": {"code": SUPERVISOR_LOST, "message": "task supervisor disappeared"},
            },
        )
        return self.store.update_state(
            run_id,
            state=TaskState.FAILED.value,
            finished_at=utc_now(),
            error={"code": SUPERVISOR_LOST, "message": "task supervisor disappeared"},
        )

    def _active_states(self) -> list[dict[str, Any]]:
        return [
            reconciled
            for state in self.store.list_states()
            if (reconciled := self._reconcile(state)).get("state") in ACTIVE_STATES
        ]

    def start_task(self, request: TaskRequest) -> dict[str, Any]:
        request = self.normalize_request(request)
        agent = self.config.agent(request.agent)

        if request.permission.rank > self.config.policy.max_permission.rank:
            return self._reject(
                request,
                agent,
                PERMISSION_DENIED,
                f"permission {request.permission.value} exceeds user policy",
            )
        if not agent.supports(request.permission):
            return self._reject(
                request,
                agent,
                UNSUPPORTED_CAPABILITY,
                f"agent {agent.name} does not support {request.permission.value}",
            )
        if request.permission is Permission.UNRESTRICTED and not agent.allow_unrestricted:
            return self._reject(
                request,
                agent,
                PERMISSION_DENIED,
                f"unrestricted mode is disabled for agent {agent.name}",
            )
        if request.files and not agent.input.file_args:
            return self._reject(
                request, agent, UNSUPPORTED_CAPABILITY, f"agent {agent.name} does not support files"
            )
        if request.images and not agent.input.image_args:
            return self._reject(
                request,
                agent,
                UNSUPPORTED_CAPABILITY,
                f"agent {agent.name} does not support images",
            )

        with self.store.registry_lock():
            active = self._active_states()
            if len(active) >= self.config.policy.max_concurrency:
                return self._reject(
                    request,
                    agent,
                    CONCURRENCY_LIMIT,
                    f"maximum concurrency of {self.config.policy.max_concurrency} reached",
                )
            if request.permission is not Permission.READ_ONLY:
                for state in active:
                    active_permission = Permission(state["permission"]["effective"])
                    if active_permission is Permission.READ_ONLY:
                        continue
                    if _paths_overlap(request.cwd, Path(state["cwd"]).resolve()):
                        return self._reject(
                            request,
                            agent,
                            WORKSPACE_BUSY,
                            f"write-capable task already owns overlapping cwd: {state['cwd']}",
                        )
            run_id = self._new_run(request, agent)
            self.store.update_state(run_id, state=TaskState.STARTING.value)

        command = [
            sys.executable,
            "-m",
            "cliexec",
            "_supervise",
            run_id,
            "--state-root",
            str(self.store.root),
        ]
        try:
            supervisor = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                env=os.environ.copy(),
            )
        except OSError as exc:
            self.store.save_result(
                run_id,
                "",
                {
                    "state": TaskState.FAILED.value,
                    "error": {"code": SPAWN_ERROR, "message": str(exc)},
                },
            )
            return self.store.update_state(
                run_id,
                state=TaskState.FAILED.value,
                finished_at=utc_now(),
                error={"code": SPAWN_ERROR, "message": f"cannot start supervisor: {exc}"},
            )
        self.store.update_state(
            run_id,
            supervisor_pid=supervisor.pid,
            supervisor_start_ticks=proc_start_ticks(supervisor.pid),
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.store.load_state(run_id)
            if state["state"] == TaskState.RUNNING.value or TaskState(state["state"]).terminal:
                return state
            if supervisor.poll() is not None:
                break
            time.sleep(0.05)
        if supervisor.poll() is None:
            supervisor.terminate()
            try:
                supervisor.wait(timeout=1)
            except subprocess.TimeoutExpired:
                supervisor.kill()
        self.store.save_result(
            run_id,
            "",
            {
                "state": TaskState.FAILED.value,
                "error": {"code": SPAWN_ERROR, "message": "supervisor startup handshake failed"},
            },
        )
        return self.store.update_state(
            run_id,
            state=TaskState.FAILED.value,
            finished_at=utc_now(),
            error={"code": SPAWN_ERROR, "message": "supervisor startup handshake failed"},
        )

    def run_task(self, request: TaskRequest) -> dict[str, Any]:
        state = self.start_task(request)
        run_id = state["run_id"]
        if TaskState(state["state"]).terminal:
            return self.result(run_id)
        try:
            while True:
                state = self.status(run_id)
                if TaskState(state["state"]).terminal:
                    return self.result(run_id)
                time.sleep(0.1)
        except KeyboardInterrupt:
            self.cancel(run_id)
            return self.result(run_id)

    def status(self, run_id: str) -> dict[str, Any]:
        state = self._reconcile(self.store.load_state(run_id))
        return {
            **state,
            "succeeded": state["state"] == TaskState.COMPLETED.value,
        }

    def result(self, run_id: str) -> dict[str, Any]:
        payload = self.store.load_request(run_id)
        return self.store.result_view(run_id, int(payload["policy"]["inline_result_bytes"]))

    def cancel(self, run_id: str) -> dict[str, Any]:
        state = self._reconcile(self.store.load_state(run_id))
        if TaskState(state["state"]).terminal:
            return state
        self.store.request_cancel(run_id)
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            state = self._reconcile(self.store.load_state(run_id))
            if TaskState(state["state"]).terminal:
                return state
            time.sleep(0.1)
        _kill_recorded_child(state)
        self.store.save_result(
            run_id,
            "",
            {
                "state": TaskState.CANCELLED.value,
                "error": {"code": "CANCELLED", "message": "task was cancelled"},
            },
        )
        return self.store.update_state(
            run_id,
            state=TaskState.CANCELLED.value,
            finished_at=utc_now(),
            error={"code": "CANCELLED", "message": "task was cancelled"},
        )

    def list_agents(self) -> list[dict[str, Any]]:
        import shutil

        result: list[dict[str, Any]] = []
        for name, agent in sorted(self.config.agents.items()):
            executable = agent.command[0]
            available = (
                bool(shutil.which(executable))
                if os.sep not in executable
                else Path(executable).exists()
            )
            result.append(
                {
                    "name": name,
                    "enabled": agent.enabled,
                    "available": available,
                    "builtin": agent.builtin,
                    "command": executable,
                    "capabilities": agent.capabilities(),
                }
            )
        return result

    def list_runs(self) -> list[dict[str, Any]]:
        return [self._reconcile(state) for state in self.store.list_states()]


def result_exit_code(value: dict[str, Any]) -> int:
    state = value.get("state")
    if state == TaskState.COMPLETED.value:
        return 0
    if state in {member.value for member in TaskState if member.terminal}:
        return 1
    return 0
