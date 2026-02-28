from __future__ import annotations

import os
import threading
from collections import deque
from pathlib import Path
from typing import Callable
from uuid import UUID


def modal_available() -> bool:
    try:
        import modal  # noqa: F401

        return True
    except ImportError:
        return False


def modal_authenticated() -> tuple[bool, str | None]:
    if not modal_available():
        return False, "modal package is not installed."

    token_id = os.getenv("MODAL_TOKEN_ID")
    token_secret = os.getenv("MODAL_TOKEN_SECRET")
    if token_id and token_secret:
        return True, None

    config_path = Path.home() / ".modal.toml"
    if config_path.exists():
        return True, None

    return (
        False,
        "No Modal credentials found. Set MODAL_TOKEN_ID/MODAL_TOKEN_SECRET or run 'modal token new'.",
    )


def run_on_modal(
    *,
    project_dir: Path,
    script_rel: Path,
    script_args: list[str],
    env: dict[str, str],
    run_id: UUID,
    on_stdout: Callable[[str], None],
    on_stderr: Callable[[str], None],
) -> tuple[int, str]:
    import modal

    requirements_path = project_dir / "requirements.txt"
    image = modal.Image.debian_slim(python_version="3.11")
    if requirements_path.exists():
        image = image.pip_install_from_requirements(str(requirements_path))
    image = (
        image.add_local_dir(str(project_dir), remote_path="/workspace", copy=True)
        .run_commands("pip install --no-deps /workspace")
    )

    app = modal.App.lookup(f"expkit-run-{run_id}", create_if_missing=True)

    cmd = ["python", "-u", script_rel.as_posix()] + (script_args or [])
    remote_env = {
        k: v
        for k, v in env.items()
        if k.startswith("EXPKIT_") or k in {"SUPABASE_DB_URL", "DATABASE_URL"}
    }

    sandbox = modal.Sandbox.create(
        *cmd,
        app=app,
        image=image,
        env=remote_env,
        workdir="/workspace",
    )

    stderr_tail: deque[str] = deque(maxlen=60)

    def read_stdout() -> None:
        for line in sandbox.stdout:
            on_stdout(line.rstrip("\n"))

    def read_stderr() -> None:
        for line in sandbox.stderr:
            text = line.rstrip("\n")
            on_stderr(text)
            stderr_tail.append(text)

    t1 = threading.Thread(target=read_stdout, daemon=True)
    t2 = threading.Thread(target=read_stderr, daemon=True)
    t1.start()
    t2.start()

    sandbox.wait()
    t1.join(timeout=5)
    t2.join(timeout=5)

    return sandbox.returncode or 0, "\n".join(stderr_tail)
