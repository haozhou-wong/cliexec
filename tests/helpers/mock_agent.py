#!/usr/bin/env python3
"""Deterministic subprocess fixture used by CLIExec integration tests."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=(
            "text",
            "json",
            "jsonl",
            "malformed-json",
            "empty",
            "environment",
            "large-output",
            "sleep",
            "spawn-child",
            "argv",
        ),
        default="text",
    )
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--sleep-seconds", type=float, default=0)
    parser.add_argument("--output-bytes", type=int, default=0)
    parser.add_argument("--pid-file", type=Path)
    parser.add_argument("--child-pid-file", type=Path)
    parser.add_argument("--env-name", action="append", default=[])
    parser.add_argument("--session-id")
    parser.add_argument("--resume")
    parser.add_argument("--session-root", type=Path)
    parser.add_argument("--output-session", action="store_true")
    parser.add_argument("prompt", nargs="?")
    return parser


def _prompt(argument: str | None) -> str:
    if argument is not None:
        return argument
    return sys.stdin.read()


def _write_pid(path: Path | None, pid: int) -> None:
    if path is None:
        return
    path.write_text(str(pid), encoding="utf-8")


def _session_prompt(args: argparse.Namespace, prompt: str) -> tuple[str | None, str]:
    session_id = args.resume or args.session_id
    if args.output_session and session_id is None:
        session_id = f"session-{os.environ['CLIEXEC_RUN_ID']}"
    if session_id is None or args.session_root is None:
        return session_id, prompt
    args.session_root.mkdir(parents=True, exist_ok=True)
    path = args.session_root / f"{session_id}.json"
    history: list[str] = []
    if args.resume and path.exists():
        history = json.loads(path.read_text(encoding="utf-8"))
    history.append(prompt)
    path.write_text(json.dumps(history), encoding="utf-8")
    return session_id, "|".join(history)


def main() -> int:
    args = _parser().parse_args()
    _write_pid(args.pid_file, os.getpid())
    prompt = _prompt(args.prompt)
    session_id, prompt = _session_prompt(args, prompt)

    if args.output_session:
        print(json.dumps({"type": "session", "session_id": session_id}), flush=True)

    if args.mode == "text":
        print(f"final:{prompt}", flush=True)
    elif args.mode == "json":
        print(json.dumps({"result": {"text": f"final:{prompt}"}}), flush=True)
    elif args.mode == "jsonl":
        print(json.dumps({"type": "progress", "text": "working"}), flush=True)
        print(
            json.dumps({"type": "result", "result": {"text": f"final:{prompt}"}}),
            flush=True,
        )
    elif args.mode == "malformed-json":
        print('{"result": ', flush=True)
    elif args.mode == "empty":
        pass
    elif args.mode == "environment":
        print(
            json.dumps({name: os.environ.get(name) for name in args.env_name}, sort_keys=True),
            flush=True,
        )
    elif args.mode == "large-output":
        sys.stdout.write("x" * args.output_bytes)
        sys.stdout.flush()
    elif args.mode == "sleep":
        time.sleep(args.sleep_seconds)
        print(f"final:{prompt}", flush=True)
    elif args.mode == "spawn-child":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(3600)"],
        )
        _write_pid(args.child_pid_file, child.pid)
        time.sleep(args.sleep_seconds or 3600)
    elif args.mode == "argv":
        print(f"argv:{prompt}", flush=True)

    return args.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
