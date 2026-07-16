from __future__ import annotations

import os
from pathlib import Path

import cliexec.service as service_module
from cliexec.config import AppConfig, PolicyConfig
from cliexec.service import TaskService
from cliexec.store import RunStore, proc_start_ticks


def _service(tmp_path: Path) -> tuple[TaskService, RunStore]:
    store = RunStore(tmp_path / "state")
    config = AppConfig(
        policy=PolicyConfig(),
        agents={},
        raw={"version": 1, "policy": {}, "agents": {}},
    )
    return TaskService(config=config, store=store), store


def _lost_run(store: RunStore, *, child_start_ticks: int) -> str:
    run_id = store.create_run(
        request={
            "agent": "mock",
            "prompt": "prompt",
            "cwd": str(store.root),
            "permission": "read_only",
            "timeout_seconds": 30,
            "files": [],
            "images": [],
        },
        agent={"name": "mock"},
        policy={"inline_result_bytes": 1024},
        config_snapshot="version = 1\n",
    )
    current_pid = os.getpid()
    store.update_state(
        run_id,
        state="running",
        supervisor_pid=999_999_999,
        supervisor_start_ticks=1,
        child_pid=current_pid,
        child_pgid=current_pid,
        child_start_ticks=child_start_ticks,
    )
    return run_id


def test_reconcile_does_not_kill_reused_child_identity(tmp_path: Path, monkeypatch) -> None:
    service, store = _service(tmp_path)
    actual_ticks = proc_start_ticks(os.getpid())
    assert actual_ticks is not None
    run_id = _lost_run(store, child_start_ticks=actual_ticks + 1)
    killed: list[int] = []
    monkeypatch.setattr(service_module, "_kill_group", killed.append)

    state = service.status(run_id)

    assert state["state"] == "failed"
    assert state["error"]["code"] == "SUPERVISOR_LOST"
    assert killed == []


def test_reconcile_kills_only_matching_child_identity(tmp_path: Path, monkeypatch) -> None:
    service, store = _service(tmp_path)
    actual_ticks = proc_start_ticks(os.getpid())
    assert actual_ticks is not None
    run_id = _lost_run(store, child_start_ticks=actual_ticks)
    killed: list[int] = []
    monkeypatch.setattr(service_module, "_kill_group", killed.append)

    state = service.status(run_id)

    assert state["state"] == "failed"
    assert state["error"]["code"] == "SUPERVISOR_LOST"
    assert killed == [os.getpid()]
