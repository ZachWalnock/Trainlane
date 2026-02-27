from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Callable


def _read_pipe(
    pipe,
    callback: Callable[[str], None],
    *,
    tail: deque[str] | None = None,
) -> None:
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            text = line.rstrip("\n")
            callback(text)
            if tail is not None:
                tail.append(text)
    finally:
        pipe.close()


def run_with_streaming(
    *,
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    on_stdout: Callable[[str], None],
    on_stderr: Callable[[str], None],
) -> tuple[int, str]:
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stderr_tail: deque[str] = deque(maxlen=60)
    t1 = threading.Thread(
        target=_read_pipe,
        args=(proc.stdout, on_stdout),
        daemon=True,
    )
    t2 = threading.Thread(
        target=_read_pipe,
        args=(proc.stderr, on_stderr),
        kwargs={"tail": stderr_tail},
        daemon=True,
    )
    t1.start()
    t2.start()

    code = proc.wait()
    t1.join(timeout=1)
    t2.join(timeout=1)

    return code, "\n".join(stderr_tail)


def docker_cmd(
    *,
    image_tag: str,
    script_rel: Path,
    env: dict[str, str],
    script_args: list[str] | None = None,
) -> list[str]:
    cmd: list[str] = ["docker", "run", "--rm", "-w", "/workspace"]
    for key, val in env.items():
        if key.startswith("EXPKIT_") or key in {"SUPABASE_DB_URL", "DATABASE_URL"}:
            cmd.extend(["-e", f"{key}={val}"])

    cmd.extend([image_tag, "python", "-u", script_rel.as_posix()])
    if script_args:
        cmd.extend(script_args)
    return cmd


def local_cmd(script_path: Path, script_args: list[str] | None = None) -> list[str]:
    cmd = [sys.executable, "-u", str(script_path)]
    if script_args:
        cmd.extend(script_args)
    return cmd
