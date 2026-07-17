from __future__ import annotations

from pathlib import Path

import pytest

from cliexec.errors import RESULT_NOT_READY, CLIExecError
from cliexec.models import TaskState
from cliexec.store import RunStore


def _create_run(store: RunStore) -> str:
    return store.create_run(
        request={
            "agent": "mock",
            "prompt": "secret prompt",
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


def test_run_store_creates_private_files(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "state")

    run_id = _create_run(store)

    paths = store.paths(run_id)
    assert paths["directory"].stat().st_mode & 0o777 == 0o700
    for name in ("request", "config", "state", "events"):
        assert paths[name].stat().st_mode & 0o777 == 0o600


def test_result_is_unavailable_before_terminal_state(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "state")
    run_id = _create_run(store)

    with pytest.raises(CLIExecError) as raised:
        store.result_view(run_id, 1024)

    assert raised.value.code == RESULT_NOT_READY
    assert raised.value.exit_code == 3


def test_result_view_truncates_inline_but_keeps_full_file(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "state")
    run_id = _create_run(store)
    full_text = "a" * 2048 + "tail"
    store.save_result(run_id, full_text, {"state": "completed", "error": None})
    store.update_state(
        run_id,
        state=TaskState.COMPLETED.value,
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
        exit_code=0,
    )

    result = store.result_view(run_id, 1024)

    assert result["succeeded"] is True
    assert result["truncated"] is True
    assert len(result["final_text"].encode()) <= 1024
    assert "truncated" in result["final_text"]
    assert result["final_text"].endswith("tail")
    assert store.paths(run_id)["result"].read_text(encoding="utf-8") == full_text


def test_failed_result_is_exposed_as_partial_text(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "state")
    run_id = _create_run(store)
    store.save_result(run_id, "partial", {"state": "failed"})
    store.update_state(
        run_id,
        state=TaskState.FAILED.value,
        finished_at="2026-01-01T00:00:00Z",
        error={"code": "NONZERO_EXIT", "message": "exit 7"},
    )

    result = store.result_view(run_id, 1024)

    assert result["succeeded"] is False
    assert result["final_text"] is None
    assert result["partial_text"] == "partial"
    assert result["error"]["code"] == "NONZERO_EXIT"


def test_public_state_exposes_lineage_but_hides_native_session(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "state")
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
        conversation_id="conversation-1",
        parent_run_id="parent-1",
        native_session_id="native-secret",
    )
    store.update_state(
        run_id,
        state=TaskState.FAILED.value,
        session_claimed=True,
        finished_at="2026-01-01T00:00:00Z",
    )

    public = store.public_state(store.load_state(run_id))

    assert public["conversation_id"] == "conversation-1"
    assert public["parent_run_id"] == "parent-1"
    assert public["resumable"] is True
    assert "native_session_id" not in public
    assert "session_claimed" not in public
    assert "continued_by_run_id" not in public


def test_legacy_state_defaults_to_not_resumable(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "state")
    run_id = _create_run(store)
    state = store.load_state(run_id)
    for field in (
        "conversation_id",
        "parent_run_id",
        "native_session_id",
        "session_claimed",
        "continued_by_run_id",
    ):
        state.pop(field)
    store._atomic_json(store.paths(run_id)["state"], state)

    public = store.public_state(store.load_state(run_id))

    assert public["conversation_id"] is None
    assert public["parent_run_id"] is None
    assert public["resumable"] is False
