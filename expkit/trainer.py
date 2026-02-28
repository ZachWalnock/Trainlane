from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

from expkit.config import load_settings
from expkit.cloud.aws_runner import AwsTrainingRequest, build_hints, run_sagemaker_training
from expkit.db import Store
from expkit.errors import RemoteExecutionError
from expkit.runtime.packager import (
    build_image,
    docker_available,
    docker_daemon_ready,
    generate_dockerfile,
)
from expkit.runtime.runner import docker_cmd, local_cmd, run_with_streaming
from expkit.runtime.server import DashboardServer


class Trainer:
    def __init__(self) -> None:
        self._active_run_id: UUID | None = None
        self._dashboard_server: DashboardServer | None = None

    def _in_remote(self) -> bool:
        return os.getenv("EXPKIT_REMOTE") == "1"

    def _store_from_env(self) -> Store:
        database_url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "Remote worker missing SUPABASE_DB_URL/DATABASE_URL in environment."
            )
        return Store(database_url=database_url)

    def _append_event(
        self,
        *,
        run_id: UUID,
        event_type: str,
        key: str | None,
        value: dict[str, Any],
        step: int | None = None,
        source: str = "sdk",
        store: Store,
    ) -> None:
        store.append_event(
            run_id=run_id,
            event_type=event_type,
            key=key,
            value=value,
            step=step,
            source=source,
        )

    def run(
        self,
        fn: Callable[..., Any],
        *args: Any,
        cloud: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        In controller mode, this launches the experiment in a remote-style worker process.
        In worker mode, it executes the function directly.
        """
        if self._in_remote():
            run_id_raw = os.getenv("EXPKIT_RUN_ID")
            if not run_id_raw:
                raise RuntimeError("EXPKIT_RUN_ID missing in remote execution.")

            run_id = UUID(run_id_raw)
            store = self._store_from_env()
            self._active_run_id = run_id

            try:
                return fn(*args, **kwargs)
            except Exception:
                tb = traceback.format_exc()
                try:
                    self._append_event(
                        run_id=run_id,
                        event_type="stderr",
                        key=None,
                        value={"line": tb},
                        step=None,
                        source="worker",
                        store=store,
                    )
                finally:
                    self._active_run_id = None
                raise
            finally:
                self._active_run_id = None

        settings = load_settings()
        store = Store(database_url=settings.database_url)
        store.init_schema()

        script_path = Path(sys.argv[0]).resolve()
        if not script_path.exists():
            raise RuntimeError(
                "Unable to locate script path. Run experiments as a file (python train.py)."
            )

        project_dir = Path.cwd().resolve()
        run_id = uuid4()
        script_args = sys.argv[1:]

        backend = "process"
        image_tag: str | None = None
        script_rel_for_docker: Path | None = None
        store.create_run(
            run_id=run_id,
            script_path=str(script_path),
            backend="initializing",
            image_tag=None,
            metadata={"argv": sys.argv[1:]},
        )

        if not self._dashboard_server:
            self._dashboard_server = DashboardServer(
                store=store,
                host=settings.host,
                port=settings.dashboard_port,
            )
        try:
            dashboard_port = self._dashboard_server.start()
        except Exception as exc:
            store.finish_run(run_id=run_id, status="failed", error_summary=str(exc))
            raise

        run_url = f"http://{settings.host}:{dashboard_port}/runs/{run_id}"
        print(f"[expkit] run_id={run_id}")
        print(f"[expkit] dashboard={run_url}")

        def emit_system(message: str) -> None:
            print(message)
            self._append_event(
                run_id=run_id,
                event_type="system",
                key="status",
                value={"message": message},
                source="controller",
                step=None,
                store=store,
            )

        def on_stdout(line: str) -> None:
            print(line)
            self._append_event(
                run_id=run_id,
                event_type="stdout",
                key=None,
                value={"line": line},
                source="worker",
                step=None,
                store=store,
            )

        def on_stderr(line: str) -> None:
            print(line, file=sys.stderr)
            self._append_event(
                run_id=run_id,
                event_type="stderr",
                key=None,
                value={"line": line},
                source="worker",
                step=None,
                store=store,
            )

        def emit_proof(key: str, payload: dict[str, Any]) -> None:
            self._append_event(
                run_id=run_id,
                event_type="proof",
                key=key,
                value=payload,
                source="controller",
                step=None,
                store=store,
            )

        emit_system("[expkit] Dashboard is live. Preparing execution backend.")

        if settings.execution_mode == "aws":
            if not settings.aws_sagemaker_role_arn:
                raise RuntimeError("EXPKIT_SAGEMAKER_ROLE_ARN is required for aws mode.")
            if not settings.aws_s3_bucket:
                raise RuntimeError("EXPKIT_AWS_S3_BUCKET is required for aws mode.")

            hints = build_hints(
                cloud,
                default_budget=settings.max_budget_usd_per_run,
                default_spot=settings.aws_use_spot,
            )

            backend = "aws-sagemaker"
            store.update_run_execution(run_id=run_id, backend=backend, image_tag=None)
            emit_system(f"[expkit] Launching worker with backend={backend}.")

            request = AwsTrainingRequest(
                run_id=run_id,
                script_path=script_path,
                project_dir=project_dir,
                script_args=script_args,
                database_url=settings.database_url,
                role_arn=settings.aws_sagemaker_role_arn,
                s3_bucket=settings.aws_s3_bucket,
                allowed_regions=settings.aws_regions,
                aws_profile=settings.aws_profile,
                expected_account_id=settings.aws_account_id,
                hints=hints,
            )

            try:
                result = run_sagemaker_training(
                    request,
                    emit_system=emit_system,
                    emit_stdout=on_stdout,
                    emit_stderr=on_stderr,
                    emit_proof=emit_proof,
                )
            except Exception as exc:
                summary = str(exc)[-1200:]
                store.finish_run(run_id=run_id, status="failed", error_summary=summary)
                if isinstance(exc, RemoteExecutionError):
                    raise
                raise RemoteExecutionError(
                    f"AWS execution failed for run {run_id}: {exc}"
                ) from exc

            emit_proof(
                "cloud_completion",
                {
                    "job_name": result.job_name,
                    "job_arn": result.job_arn,
                    "region": result.region,
                    "instance_type": result.instance_type,
                    "model_artifact_s3_uri": result.model_artifact_s3_uri,
                },
            )
            store.finish_run(run_id=run_id, status="succeeded")
            print(f"[expkit] run completed successfully: {run_id}")
            return

        requirements_exists = (project_dir / "requirements.txt").exists()
        if settings.docker_enabled and requirements_exists:
            if not docker_available():
                emit_system("[expkit] Docker CLI not found; using process backend.")
            else:
                daemon_ready, daemon_reason = docker_daemon_ready()
                if not daemon_ready:
                    emit_system(
                        "[expkit] Docker daemon unavailable; using process backend. "
                        f"Reason: {daemon_reason}"
                    )
                else:
                    try:
                        emit_system("[expkit] Docker daemon ready. Building runtime image.")
                        dockerfile = generate_dockerfile(project_dir=project_dir, run_id=run_id)
                        image_tag = build_image(
                            project_dir=project_dir,
                            dockerfile=dockerfile,
                            run_id=run_id,
                        )
                        try:
                            script_rel_for_docker = script_path.relative_to(project_dir)
                            backend = "docker"
                            emit_system("[expkit] Docker image build complete.")
                        except ValueError:
                            backend = "process"
                            image_tag = None
                            emit_system(
                                "[expkit] Script path is outside project directory; "
                                "using process backend."
                            )
                    except Exception as exc:
                        backend = "process"
                        image_tag = None
                        emit_system(
                            "[expkit] Docker image build failed; using process backend. "
                            f"Reason: {exc}"
                        )
        elif settings.docker_enabled and not requirements_exists:
            emit_system("[expkit] requirements.txt not found; using process backend.")
        elif not settings.docker_enabled:
            emit_system("[expkit] Docker disabled via EXPKIT_DOCKER=0; using process backend.")

        store.update_run_execution(run_id=run_id, backend=backend, image_tag=image_tag)
        emit_system(f"[expkit] Launching worker with backend={backend}.")

        base_env = os.environ.copy()
        base_env["EXPKIT_REMOTE"] = "1"
        base_env["EXPKIT_RUN_ID"] = str(run_id)
        base_env["SUPABASE_DB_URL"] = settings.database_url

        if backend == "docker" and image_tag and script_rel_for_docker:
            cmd = docker_cmd(
                image_tag=image_tag,
                script_rel=script_rel_for_docker,
                env=base_env,
                script_args=script_args,
            )
        else:
            cmd = local_cmd(script_path, script_args)

        code, stderr_tail = run_with_streaming(
            cmd=cmd,
            cwd=project_dir,
            env=base_env,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )

        if code == 0:
            store.finish_run(run_id=run_id, status="succeeded")
            print(f"[expkit] run completed successfully: {run_id}")
            return

        summary = (stderr_tail or "remote worker failed")[-800:]
        store.finish_run(run_id=run_id, status="failed", error_summary=summary)
        raise RemoteExecutionError(
            f"Remote execution failed with exit code {code}.\n"
            f"Run ID: {run_id}\n"
            f"Dashboard: {run_url}\n"
            f"--- stderr tail ---\n{stderr_tail or '(no stderr)'}"
        )

    def log(
        self,
        key: str,
        value: Any,
        *,
        step: int | None = None,
        kind: str = "metric",
        **extra: Any,
    ) -> None:
        run_id = self._active_run_id
        if run_id is None and os.getenv("EXPKIT_RUN_ID"):
            run_id = UUID(os.getenv("EXPKIT_RUN_ID", ""))

        if run_id is None:
            raise RuntimeError("trainer.log() called outside an active expkit run.")

        store = self._store_from_env()
        payload = {"value": value}
        if step is not None:
            payload["step"] = step
        payload.update(extra)

        self._append_event(
            run_id=run_id,
            event_type=kind,
            key=key,
            value=payload,
            step=step,
            source="worker",
            store=store,
        )

        # Echo metrics to stdout so agents and users can observe progress in terminal.
        print(f"[metric] {key}={value} step={step}")
