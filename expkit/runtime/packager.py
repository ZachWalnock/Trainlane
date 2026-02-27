from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from uuid import UUID


def docker_available() -> bool:
    return shutil.which("docker") is not None


def docker_daemon_ready(timeout_seconds: float = 2.0) -> tuple[bool, str | None]:
    if not docker_available():
        return False, "Docker CLI is not installed."

    try:
        result = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "Timed out while checking Docker daemon."
    except Exception as exc:
        return False, f"Unable to check Docker daemon: {exc}"

    if result.returncode == 0:
        return True, None

    stderr_lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
    reason = "Docker daemon is not reachable."
    for line in stderr_lines:
        if "Cannot connect to the Docker daemon" in line:
            reason = line
            break
    if reason == "Docker daemon is not reachable." and stderr_lines:
        reason = stderr_lines[0]
    return False, reason


def generate_dockerfile(project_dir: Path, run_id: UUID) -> Path:
    build_dir = project_dir / ".expkit" / "build" / str(run_id)
    build_dir.mkdir(parents=True, exist_ok=True)
    dockerfile = build_dir / "Dockerfile.generated"

    content = """
FROM python:3.11-slim
WORKDIR /workspace
COPY requirements.txt /workspace/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . /workspace
ENV PYTHONUNBUFFERED=1
""".strip()
    dockerfile.write_text(content + "\n", encoding="utf-8")
    return dockerfile


def build_image(project_dir: Path, dockerfile: Path, run_id: UUID) -> str:
    image_tag = f"expkit-run-{run_id}"
    cmd = [
        "docker",
        "build",
        "-f",
        str(dockerfile),
        "-t",
        image_tag,
        str(project_dir),
    ]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr_lines = (result.stderr or "").strip().splitlines()
        stdout_lines = (result.stdout or "").strip().splitlines()
        snippet = "\n".join((stderr_lines or stdout_lines)[-8:])
        raise RuntimeError(snippet or "docker build command failed")
    return image_tag
