from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO

from .adapter import best_effort_partial, build_command, parse_output, parse_session_id
from .config import AgentConfig
from .errors import (
    CANCELLED,
    NONZERO_EXIT,
    OUTPUT_LIMIT,
    PROTOCOL_ERROR,
    SPAWN_ERROR,
    TIMEOUT,
    CLIExecError,
)
from .models import TaskRequest, TaskState
from .store import RunStore, proc_start_ticks
from .util import permission_mode, utc_now

_CHUNK_SIZE = 64 * 1024


class _OutputCapture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.stdout_bytes = 0
        self.stderr_bytes = 0
        self.last_activity = utc_now()
        self.limit_reached = threading.Event()
        self._lock = threading.Lock()

    def copy(self, source: BinaryIO, destination: BinaryIO, stream: str) -> None:
        while True:
            chunk = source.read(_CHUNK_SIZE)
            if not chunk:
                break
            with self._lock:
                total = self.stdout_bytes + self.stderr_bytes
                remaining = max(0, self.limit - total)
                accepted = chunk[:remaining]
                if stream == "stdout":
                    self.stdout_bytes += len(accepted)
                else:
                    self.stderr_bytes += len(accepted)
                self.last_activity = utc_now()
                if len(accepted) < len(chunk) or remaining == 0:
                    self.limit_reached.set()
            if accepted:
                destination.write(accepted)
                destination.flush()
            if self.limit_reached.is_set():
                break

    def snapshot(self) -> tuple[int, int, str]:
        with self._lock:
            return self.stdout_bytes, self.stderr_bytes, self.last_activity


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_group(
    pgid: int, grace: float = 5.0, process: subprocess.Popen[bytes] | None = None
) -> None:
    if not _group_exists(pgid):
        return
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if process is not None:
            process.poll()
        if not _group_exists(pgid):
            return
        time.sleep(0.05)
    _signal_group(pgid, signal.SIGKILL)


def _write_prompt(pipe: BinaryIO, prompt: str) -> None:
    try:
        pipe.write(prompt.encode("utf-8"))
        pipe.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        with suppress(OSError):
            pipe.close()


def _finish_error(
    store: RunStore,
    run_id: str,
    *,
    code: str,
    message: str,
    exit_code: int | None,
    stdout_bytes: int = 0,
    stderr_bytes: int = 0,
    partial: str | None = None,
    state: TaskState = TaskState.FAILED,
) -> None:
    store.save_result(
        run_id,
        partial or "",
        {"state": state.value, "error": {"code": code, "message": message}},
    )
    store.update_state(
        run_id,
        state=state.value,
        finished_at=utc_now(),
        exit_code=exit_code,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        error={"code": code, "message": message},
    )
    store.release_unclaimed_continuation(run_id)


def supervise(run_id: str, root: Path) -> int:
    store = RunStore(root)
    process: subprocess.Popen[bytes] | None = None
    child_pgid: int | None = None
    termination_requested = threading.Event()

    def handle_supervisor_signal(_signum: int, _frame: object) -> None:
        termination_requested.set()

    signal.signal(signal.SIGTERM, handle_supervisor_signal)
    signal.signal(signal.SIGINT, handle_supervisor_signal)

    try:
        payload = store.load_request(run_id)
        request = TaskRequest.from_dict(payload["request"])
        agent = AgentConfig.from_dict(payload["agent_config"])
        policy = payload["policy"]
        store.update_state(
            run_id,
            state=TaskState.STARTING.value,
            supervisor_pid=os.getpid(),
            supervisor_start_ticks=proc_start_ticks(os.getpid()),
        )
        initial_state = store.load_state(run_id)
        native_session_id = initial_state.get("native_session_id")
        resume = initial_state.get("parent_run_id") is not None
        spec = build_command(
            agent,
            request,
            run_id=run_id,
            native_session_id=(str(native_session_id) if native_session_id is not None else None),
            resume=resume,
        )
        paths = store.paths(run_id)
        for key in ("stdout", "stderr"):
            paths[key].touch(mode=0o600, exist_ok=True)
            permission_mode(paths[key], 0o600)

        with paths["stdout"].open("wb") as stdout_file, paths["stderr"].open("wb") as stderr_file:
            try:
                process = subprocess.Popen(
                    spec.argv,
                    cwd=spec.cwd,
                    env=spec.env,
                    stdin=subprocess.PIPE if spec.stdin_text is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as exc:
                _finish_error(
                    store,
                    run_id,
                    code=SPAWN_ERROR,
                    message=f"cannot start agent: {exc}",
                    exit_code=None,
                )
                return 1
            child_pgid = process.pid
            started_at = utc_now()
            state_changes: dict[str, object] = {}
            if agent.session is not None and (resume or agent.session.id_strategy == "generated"):
                state_changes["session_claimed"] = True
            store.update_state(
                run_id,
                state=TaskState.RUNNING.value,
                started_at=started_at,
                last_activity_at=started_at,
                child_pid=process.pid,
                child_pgid=child_pgid,
                child_start_ticks=proc_start_ticks(process.pid),
                **state_changes,
            )

            capture = _OutputCapture(int(policy["max_output_bytes"]))
            assert process.stdout is not None and process.stderr is not None
            stdout_thread = threading.Thread(
                target=capture.copy,
                args=(process.stdout, stdout_file, "stdout"),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=capture.copy,
                args=(process.stderr, stderr_file, "stderr"),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            if spec.stdin_text is not None:
                assert process.stdin is not None
                threading.Thread(
                    target=_write_prompt,
                    args=(process.stdin, spec.stdin_text),
                    daemon=True,
                ).start()

            deadline = time.monotonic() + request.timeout_seconds
            reason: str | None = None
            last_persisted = (-1, -1)
            while process.poll() is None:
                stdout_bytes, stderr_bytes, last_activity = capture.snapshot()
                if (stdout_bytes, stderr_bytes) != last_persisted:
                    store.update_state(
                        run_id,
                        stdout_bytes=stdout_bytes,
                        stderr_bytes=stderr_bytes,
                        last_activity_at=last_activity,
                    )
                    last_persisted = (stdout_bytes, stderr_bytes)
                state = store.load_state(run_id)
                if termination_requested.is_set() or state.get("cancel_requested"):
                    reason = CANCELLED
                    break
                if capture.limit_reached.is_set():
                    reason = OUTPUT_LIMIT
                    break
                if time.monotonic() >= deadline:
                    reason = TIMEOUT
                    break
                time.sleep(0.1)

            if reason is not None and child_pgid is not None:
                _terminate_group(child_pgid, process=process)
            exit_code = process.wait()
            if child_pgid is not None and _group_exists(child_pgid):
                _terminate_group(child_pgid, grace=1.0, process=process)
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
            stdout_bytes, stderr_bytes, last_activity = capture.snapshot()
            store.update_state(
                run_id,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                last_activity_at=last_activity,
            )

        if reason is None and capture.limit_reached.is_set():
            reason = OUTPUT_LIMIT

        session_error: CLIExecError | None = None
        session_mismatch = False
        if agent.session is not None and agent.session.id_strategy == "output":
            try:
                emitted_session_id = parse_session_id(paths["stdout"], agent)
                if native_session_id is not None and emitted_session_id != native_session_id:
                    session_mismatch = True
                    raise CLIExecError(PROTOCOL_ERROR, "worker resumed a different session ID")
                native_session_id = emitted_session_id
                store.update_state(
                    run_id,
                    native_session_id=emitted_session_id,
                    session_claimed=True,
                )
            except CLIExecError as exc:
                session_error = exc

        if session_mismatch:
            store.update_state(run_id, session_claimed=False)
            partial = best_effort_partial(paths["stdout"], agent)
            _finish_error(
                store,
                run_id,
                code=PROTOCOL_ERROR,
                message="worker resumed a different session ID",
                exit_code=exit_code,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                partial=partial,
            )
            return 1

        if reason is not None:
            partial = best_effort_partial(paths["stdout"], agent)
            state = {
                CANCELLED: TaskState.CANCELLED,
                TIMEOUT: TaskState.TIMED_OUT,
                OUTPUT_LIMIT: TaskState.FAILED,
            }[reason]
            message = {
                CANCELLED: "task was cancelled",
                TIMEOUT: "task exceeded its timeout",
                OUTPUT_LIMIT: "task exceeded the combined output limit",
            }[reason]
            _finish_error(
                store,
                run_id,
                code=reason,
                message=message,
                exit_code=exit_code,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                partial=partial,
                state=state,
            )
            return 1

        if exit_code not in agent.success_exit_codes:
            partial = best_effort_partial(paths["stdout"], agent)
            _finish_error(
                store,
                run_id,
                code=NONZERO_EXIT,
                message=f"agent exited with code {exit_code}",
                exit_code=exit_code,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                partial=partial,
            )
            return 1

        if session_error is not None:
            partial = best_effort_partial(paths["stdout"], agent)
            _finish_error(
                store,
                run_id,
                code=PROTOCOL_ERROR,
                message=session_error.message,
                exit_code=exit_code,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                partial=partial,
            )
            return 1

        try:
            final_text = parse_output(paths["stdout"], agent)
        except CLIExecError as exc:
            partial = best_effort_partial(paths["stdout"], agent)
            _finish_error(
                store,
                run_id,
                code=exc.code if exc.code == PROTOCOL_ERROR else PROTOCOL_ERROR,
                message=exc.message,
                exit_code=exit_code,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                partial=partial,
            )
            return 1

        store.save_result(run_id, final_text, {"state": TaskState.COMPLETED.value, "error": None})
        store.update_state(
            run_id,
            state=TaskState.COMPLETED.value,
            finished_at=utc_now(),
            exit_code=exit_code,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            error=None,
        )
        return 0
    except CLIExecError as exc:
        with suppress(CLIExecError):
            _finish_error(
                store,
                run_id,
                code=exc.code,
                message=exc.message,
                exit_code=process.returncode if process else None,
            )
        return 1
    except Exception as exc:  # pragma: no cover - last-resort state preservation
        with suppress(CLIExecError):
            _finish_error(
                store,
                run_id,
                code=SPAWN_ERROR,
                message=f"supervisor failed: {type(exc).__name__}: {exc}",
                exit_code=process.returncode if process else None,
            )
        return 1
    finally:
        if process is not None and process.poll() is None and child_pgid is not None:
            _terminate_group(child_pgid, process=process)
