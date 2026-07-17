from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AgentConfig, AppConfig, PolicyConfig, load_config, toml_dumps
from .errors import (
    CONCURRENCY_LIMIT,
    CONVERSATION_CONFLICT,
    INVALID_REQUEST,
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

    def normalize_request(
        self, request: TaskRequest, parent_state: dict[str, Any] | None = None
    ) -> TaskRequest:
        try:
            depth = int(os.environ.get("CLIEXEC_DEPTH", "0"))
        except ValueError:
            depth = 1
        if depth >= 1:
            raise CLIExecError(NESTED_DELEGATION, "nested CLIExec delegation is disabled")
        requested_cwd = request.cwd
        if requested_cwd is None:
            requested_cwd = Path(parent_state["cwd"]) if parent_state is not None else Path.cwd()
        cwd = requested_cwd.expanduser().resolve()
        if not cwd.is_dir():
            raise CLIExecError(INVALID_REQUEST, f"cwd is not a directory: {cwd}")
        if parent_state is not None and cwd != Path(parent_state["cwd"]).resolve():
            raise CLIExecError(
                INVALID_REQUEST,
                f"continued runs must use the original cwd: {parent_state['cwd']}",
            )
        prompt = request.prompt
        if not prompt.strip():
            raise CLIExecError(INVALID_REQUEST, "prompt cannot be empty")
        files = [self._resolve_input_path(path, cwd, "file") for path in request.files]
        images = [self._resolve_input_path(path, cwd, "image") for path in request.images]
        if request.timeout_seconds <= 0:
            raise CLIExecError(INVALID_REQUEST, "timeout must be positive")
        if request.timeout_seconds > self.config.policy.max_timeout:
            raise CLIExecError(
                INVALID_REQUEST,
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
            continue_run_id=request.continue_run_id,
        )

    @staticmethod
    def _resolve_input_path(path: Path, cwd: Path, kind: str) -> Path:
        resolved = path.expanduser()
        if not resolved.is_absolute():
            resolved = cwd / resolved
        resolved = resolved.resolve()
        if not resolved.is_file():
            raise CLIExecError(INVALID_REQUEST, f"{kind} does not exist: {resolved}")
        return resolved

    def _new_run(
        self,
        request: TaskRequest,
        agent: AgentConfig,
        *,
        conversation_id: str | None = None,
        parent_run_id: str | None = None,
        native_session_id: str | None = None,
    ) -> str:
        return self.store.create_run(
            request=request.to_dict(),
            agent=agent.to_dict(),
            policy=_policy_dict(self.config.policy),
            config_snapshot=toml_dumps(self.config.raw),
            conversation_id=conversation_id,
            parent_run_id=parent_run_id,
            native_session_id=native_session_id,
        )

    def _reject(
        self,
        request: TaskRequest,
        agent: AgentConfig,
        code: str,
        message: str,
        *,
        conversation_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> dict[str, Any]:
        run_id = self._new_run(
            request,
            agent,
            conversation_id=conversation_id,
            parent_run_id=parent_run_id,
        )
        self.store.save_result(
            run_id,
            "",
            {"state": TaskState.REJECTED.value, "error": {"code": code, "message": message}},
        )
        return self.store.public_state(
            self.store.update_state(
                run_id,
                state=TaskState.REJECTED.value,
                finished_at=utc_now(),
                error={"code": code, "message": message},
            )
        )

    def _reconcile(self, state: dict[str, Any], *, registry_locked: bool = False) -> dict[str, Any]:
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
        failed = self.store.update_state(
            run_id,
            state=TaskState.FAILED.value,
            finished_at=utc_now(),
            error={"code": SUPERVISOR_LOST, "message": "task supervisor disappeared"},
        )
        self.store.release_unclaimed_continuation(run_id, registry_locked=registry_locked)
        return failed

    def _active_states(self, *, registry_locked: bool = False) -> list[dict[str, Any]]:
        return [
            reconciled
            for state in self.store.list_states()
            if (reconciled := self._reconcile(state, registry_locked=registry_locked)).get("state")
            in ACTIVE_STATES
        ]

    @staticmethod
    def _continuation_problem(
        parent: dict[str, Any], request: TaskRequest, agent: AgentConfig
    ) -> tuple[str, str] | None:
        if parent.get("agent") != request.agent:
            return (
                INVALID_REQUEST,
                f"continued run uses agent {parent.get('agent')}, not {request.agent}",
            )
        if agent.session is None:
            return (
                UNSUPPORTED_CAPABILITY,
                f"agent {agent.name} does not support sessions",
            )
        parent_state = TaskState(parent["state"])
        if not parent_state.terminal:
            return (
                CONVERSATION_CONFLICT,
                f"run {parent['run_id']} is still {parent_state.value}",
            )
        if parent_state is TaskState.REJECTED:
            return (
                UNSUPPORTED_CAPABILITY,
                f"run {parent['run_id']} did not create a resumable session",
            )
        if not (
            parent.get("conversation_id")
            and parent.get("native_session_id")
            and parent.get("session_claimed")
        ):
            return (
                UNSUPPORTED_CAPABILITY,
                f"run {parent['run_id']} has no resumable session",
            )
        if parent.get("continued_by_run_id"):
            return (
                CONVERSATION_CONFLICT,
                f"run {parent['run_id']} is not the latest conversation tip",
            )
        return None

    def start_task(self, request: TaskRequest) -> dict[str, Any]:
        parent = None
        if request.continue_run_id is not None:
            parent = self._reconcile(self.store.load_state(request.continue_run_id))
        request = self.normalize_request(request, parent)
        agent = self.config.agent(request.agent)
        conversation_id = (
            str(parent["conversation_id"])
            if parent is not None and parent.get("conversation_id") is not None
            else None
        )
        parent_run_id = request.continue_run_id

        if request.permission.rank > self.config.policy.max_permission.rank:
            return self._reject(
                request,
                agent,
                PERMISSION_DENIED,
                f"permission {request.permission.value} exceeds user policy",
                conversation_id=conversation_id,
                parent_run_id=parent_run_id,
            )
        if not agent.supports(request.permission):
            return self._reject(
                request,
                agent,
                UNSUPPORTED_CAPABILITY,
                f"agent {agent.name} does not support {request.permission.value}",
                conversation_id=conversation_id,
                parent_run_id=parent_run_id,
            )
        if request.permission is Permission.UNRESTRICTED and not agent.allow_unrestricted:
            return self._reject(
                request,
                agent,
                PERMISSION_DENIED,
                f"unrestricted mode is disabled for agent {agent.name}",
                conversation_id=conversation_id,
                parent_run_id=parent_run_id,
            )
        if request.files and not agent.input.file_args:
            return self._reject(
                request,
                agent,
                UNSUPPORTED_CAPABILITY,
                f"agent {agent.name} does not support files",
                conversation_id=conversation_id,
                parent_run_id=parent_run_id,
            )
        if request.images and not agent.input.image_args:
            return self._reject(
                request,
                agent,
                UNSUPPORTED_CAPABILITY,
                f"agent {agent.name} does not support images",
                conversation_id=conversation_id,
                parent_run_id=parent_run_id,
            )

        with self.store.registry_lock():
            if parent_run_id is not None:
                parent = self._reconcile(self.store.load_state(parent_run_id), registry_locked=True)
                conversation_id = (
                    str(parent["conversation_id"])
                    if parent.get("conversation_id") is not None
                    else None
                )
                if problem := self._continuation_problem(parent, request, agent):
                    code, message = problem
                    return self._reject(
                        request,
                        agent,
                        code,
                        message,
                        conversation_id=conversation_id,
                        parent_run_id=parent_run_id,
                    )

            active = self._active_states(registry_locked=True)
            if len(active) >= self.config.policy.max_concurrency:
                return self._reject(
                    request,
                    agent,
                    CONCURRENCY_LIMIT,
                    f"maximum concurrency of {self.config.policy.max_concurrency} reached",
                    conversation_id=conversation_id,
                    parent_run_id=parent_run_id,
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
                            conversation_id=conversation_id,
                            parent_run_id=parent_run_id,
                        )
            native_session_id: str | None = None
            if parent is not None:
                native_session_id = str(parent["native_session_id"])
            elif agent.session is not None:
                conversation_id = str(uuid.uuid4())
                if agent.session.id_strategy == "generated":
                    native_session_id = str(uuid.uuid4())
            run_id = self._new_run(
                request,
                agent,
                conversation_id=conversation_id,
                parent_run_id=parent_run_id,
                native_session_id=native_session_id,
            )
            if parent_run_id is not None:
                self.store.update_state(parent_run_id, continued_by_run_id=run_id)
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
            failed = self.store.update_state(
                run_id,
                state=TaskState.FAILED.value,
                finished_at=utc_now(),
                error={"code": SPAWN_ERROR, "message": f"cannot start supervisor: {exc}"},
            )
            self.store.release_unclaimed_continuation(run_id)
            return self.store.public_state(failed)
        self.store.update_state(
            run_id,
            supervisor_pid=supervisor.pid,
            supervisor_start_ticks=proc_start_ticks(supervisor.pid),
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.store.load_state(run_id)
            if state["state"] == TaskState.RUNNING.value or TaskState(state["state"]).terminal:
                return self.store.public_state(state)
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
        failed = self.store.update_state(
            run_id,
            state=TaskState.FAILED.value,
            finished_at=utc_now(),
            error={"code": SPAWN_ERROR, "message": "supervisor startup handshake failed"},
        )
        self.store.release_unclaimed_continuation(run_id)
        return self.store.public_state(failed)

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
            **self.store.public_state(state),
            "succeeded": state["state"] == TaskState.COMPLETED.value,
        }

    def result(self, run_id: str) -> dict[str, Any]:
        payload = self.store.load_request(run_id)
        return self.store.result_view(run_id, int(payload["policy"]["inline_result_bytes"]))

    def cancel(self, run_id: str) -> dict[str, Any]:
        state = self._reconcile(self.store.load_state(run_id))
        if TaskState(state["state"]).terminal:
            return self.store.public_state(state)
        self.store.request_cancel(run_id)
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            state = self._reconcile(self.store.load_state(run_id))
            if TaskState(state["state"]).terminal:
                return self.store.public_state(state)
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
        cancelled = self.store.update_state(
            run_id,
            state=TaskState.CANCELLED.value,
            finished_at=utc_now(),
            error={"code": "CANCELLED", "message": "task was cancelled"},
        )
        self.store.release_unclaimed_continuation(run_id)
        return self.store.public_state(cancelled)

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
        return [
            self.store.public_state(self._reconcile(state)) for state in self.store.list_states()
        ]


def result_exit_code(value: dict[str, Any]) -> int:
    state = value.get("state")
    if state == TaskState.COMPLETED.value:
        return 0
    if state in {member.value for member in TaskState if member.terminal}:
        return 1
    return 0
