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


def main() -> int:
    args = _parser().parse_args()
    _write_pid(args.pid_file, os.getpid())
    prompt = _prompt(args.prompt)

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
