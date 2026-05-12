#!/usr/bin/env python3
"""Run real cterm debug prompt tests and save full output."""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESULTS_PATH = ROOT / "test_results.txt"
TIMEOUT_SECONDS = 600

PROMPTS = [
    "install spotify",
    "remove spotify",
    "create folder sallyy",
    "delete folder sallyy",
    "create new hello world.py that prints hello world",
    "commit changes with message hello",
    "reverse last commit",
]


def write_section(handle, title: str) -> None:
    handle.write("\n" + "=" * 80 + "\n")
    handle.write(title + "\n")
    handle.write("=" * 80 + "\n")
    handle.flush()


def run_prompt(prompt: str) -> tuple[int, float, str]:
    start = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "cterm", "-d", prompt],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
        return result.returncode, time.monotonic() - start, result.stdout
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        return 124, time.monotonic() - start, output + "\n[TIMEOUT]\n"


def main() -> int:
    failures = 0

    with RESULTS_PATH.open("w", encoding="utf-8") as handle:
        write_section(
            handle,
            f"cterm real prompt test run started {datetime.now().isoformat(timespec='seconds')}",
        )
        handle.write(f"Command prefix: {sys.executable} -m cterm -d\n")
        handle.write(f"Working directory: {ROOT}\n")
        handle.write(f"Timeout per prompt: {TIMEOUT_SECONDS}s\n")
        handle.flush()

        for index, prompt in enumerate(PROMPTS, start=1):
            write_section(handle, f"TEST {index}/{len(PROMPTS)}: {prompt}")
            returncode, duration, output = run_prompt(prompt)
            handle.write(f"Return code: {returncode}\n")
            handle.write(f"Duration: {duration:.2f}s\n")
            handle.write("--- output ---\n")
            handle.write(output)
            if output and not output.endswith("\n"):
                handle.write("\n")
            handle.write("--- end output ---\n")
            handle.flush()

            if returncode != 0:
                failures += 1

        write_section(
            handle,
            f"cterm real prompt test run finished {datetime.now().isoformat(timespec='seconds')}",
        )
        handle.write(f"Total prompts: {len(PROMPTS)}\n")
        handle.write(f"Failures: {failures}\n")

    print(f"Wrote results to {RESULTS_PATH}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
