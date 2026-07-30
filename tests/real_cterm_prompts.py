#!/usr/bin/env python3
"""Run real cterm prompt tests and save full output."""

from __future__ import annotations

import subprocess
import sys
import time
import re
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESULTS_PATH = ROOT / "test_results.txt"
TIMEOUT_SECONDS = 600
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
SPINNER_PREFIXES = ("⠋ ", "⠙ ", "⠹ ", "⠸ ", "⠼ ", "⠴ ", "⠦ ", "⠧ ", "⠇ ", "⠏ ")

PROMPTS = [
    "install spotify",
    "remove spotify",
    "create folder sallyy",
    "delete folder sallyy",
    "create new hello world.py that prints hello world",
    "commit hello_world.py with message hello",
    "undo commit hello",
    "CPU count",
    "VRAM usage",
    "which nividia driver",
    "desktop env",
    "which display server",
    "what's in my recycle bin",
    "what VPNs present"

]


def write_section(handle, title: str) -> None:
    handle.write("\n" + "=" * 80 + "\n")
    handle.write(title + "\n")
    handle.write("=" * 80 + "\n")
    handle.flush()


def normalize_output(raw: str) -> str:
    text = ANSI_RE.sub("", raw)
    lines: list[str] = []
    current: list[str] = []

    for char in text:
        if char == "\r":
            current = []
        elif char == "\n":
            line = "".join(current)
            if line:
                lines.append(line)
            current = []
        else:
            current.append(char)

    tail = "".join(current)
    if tail:
        lines.append(tail)

    filtered = [line for line in lines if not line.startswith(SPINNER_PREFIXES)]
    return "\n".join(filtered).strip()


def run_prompt(prompt: str) -> tuple[int, float, str]:
    start = time.monotonic()
    lines: list[str] = []
    last_spinner_text: str | None = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "cterm", prompt],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            lines.append(line)
            if line.startswith(SPINNER_PREFIXES):
                matched = next(p for p in SPINNER_PREFIXES if line.startswith(p))
                rest = line[len(matched):]
                if rest == last_spinner_text:
                    continue
                last_spinner_text = rest
            print(line, end="", flush=True)
        proc.wait(timeout=TIMEOUT_SECONDS)
        return proc.returncode, time.monotonic() - start, "".join(lines)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        output = "".join(lines)
        return 124, time.monotonic() - start, output + "\n[TIMEOUT]\n"


def main() -> int:
    failures = 0

    with RESULTS_PATH.open("w", encoding="utf-8") as handle:
        write_section(
            handle,
            f"cterm real prompt test run started {datetime.now().isoformat(timespec='seconds')}",
        )
        handle.write(f"Command prefix: {sys.executable} -m cterm\n")
        handle.write(f"Working directory: {ROOT}\n")
        handle.write(f"Timeout per prompt: {TIMEOUT_SECONDS}s\n")
        handle.flush()

        for index, prompt in enumerate(PROMPTS, start=1):
            title = f"TEST {index}/{len(PROMPTS)}: {prompt}"
            write_section(handle, title)
            print(f"\n{'=' * 80}\n{title}\n{'=' * 80}")
            returncode, duration, output = run_prompt(prompt)
            handle.write(f"Return code: {returncode}\n")
            handle.write(f"Duration: {duration:.2f}s\n")
            handle.write("--- normalized output ---\n")
            output = normalize_output(output)
            handle.write(output)
            if output and not output.endswith("\n"):
                handle.write("\n")
            handle.write("--- end normalized output ---\n")
            handle.flush()

            print(f"Return code: {returncode}")

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
