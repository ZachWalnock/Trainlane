from __future__ import annotations

import json
import os
import re
import tarfile
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from expkit.errors import RemoteExecutionError


SYSTEM = Callable[[str], None]
LINE = Callable[[str], None]
PROOF = Callable[[str, dict[str, Any]], None]
ROLE_ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/.+$")


GPU_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "instance_type": "ml.g5.xlarge",
        "gpu": "A10G",
        "gpu_memory_gb": 24,
        "speed_score": 1.0,
        "on_demand_usd": 1.41,
    },
    {
        "instance_type": "ml.g5.2xlarge",
        "gpu": "A10G",
        "gpu_memory_gb": 24,
        "speed_score": 1.15,
        "on_demand_usd": 1.68,
    },
    {
        "instance_type": "ml.g5.4xlarge",
        "gpu": "A10G",
        "gpu_memory_gb": 24,
        "speed_score": 1.4,
        "on_demand_usd": 2.45,
    },
    {
        "instance_type": "ml.g5.8xlarge",
        "gpu": "A10G",
        "gpu_memory_gb": 24,
        "speed_score": 1.9,
        "on_demand_usd": 4.74,
    },
    {
        "instance_type": "ml.g5.12xlarge",
        "gpu": "A10Gx4",
        "gpu_memory_gb": 96,
        "speed_score": 3.2,
        "on_demand_usd": 6.72,
    },
    {
        "instance_type": "ml.p4d.24xlarge",
        "gpu": "A100x8",
        "gpu_memory_gb": 320,
        "speed_score": 7.0,
        "on_demand_usd": 32.77,
    },
)

CPU_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "instance_type": "ml.m5.large",
        "cpu": "2 vCPU",
        "memory_gb": 8,
        "speed_score": 1.0,
        "on_demand_usd": 0.134,
    },
    {
        "instance_type": "ml.m5.xlarge",
        "cpu": "4 vCPU",
        "memory_gb": 16,
        "speed_score": 1.6,
        "on_demand_usd": 0.269,
    },
    {
        "instance_type": "ml.c5.xlarge",
        "cpu": "4 vCPU",
        "memory_gb": 8,
        "speed_score": 1.9,
        "on_demand_usd": 0.238,
    },
    {
        "instance_type": "ml.c5.2xlarge",
        "cpu": "8 vCPU",
        "memory_gb": 16,
        "speed_score": 2.6,
        "on_demand_usd": 0.476,
    },
)


@dataclass(slots=True)
class CloudHints:
    model_size_b: float = 7.0
    dataset_size_gb: float = 1.0
    speed: str = "balanced"
    max_budget_usd: float | None = None
    use_spot: bool = False
    dataset_path: Path | None = None
    compute: str = "cpu"


@dataclass(slots=True)
class AwsTrainingRequest:
    run_id: UUID
    script_path: Path
    project_dir: Path
    script_args: list[str]
    database_url: str
    role_arn: str
    s3_bucket: str
    allowed_regions: tuple[str, ...]
    aws_profile: str | None
    expected_account_id: str | None
    hints: CloudHints
    max_run_seconds: int = 7200
    max_wait_seconds: int = 10800
    volume_size_gb: int = 200


@dataclass(slots=True)
class Selection:
    region: str
    instance_type: str
    hourly_usd: float
    estimated_hours: float
    estimated_cost: float
    gpu: str
    gpu_memory_gb: int
    speed_score: float


@dataclass(slots=True)
class AwsTrainingResult:
    job_name: str
    job_arn: str
    region: str
    instance_type: str
    model_artifact_s3_uri: str | None


def build_hints(cloud: dict[str, Any] | None, *, default_budget: float | None, default_spot: bool) -> CloudHints:
    cloud = cloud or {}

    model_size_b = float(cloud.get("model_size_b", os.getenv("EXPKIT_MODEL_SIZE_B", "7")))
    dataset_size_gb = float(cloud.get("dataset_size_gb", os.getenv("EXPKIT_DATASET_SIZE_GB", "1")))
    speed = str(cloud.get("speed", os.getenv("EXPKIT_SPEED", "balanced"))).strip().lower()
    compute = str(cloud.get("compute", os.getenv("EXPKIT_AWS_COMPUTE", "cpu"))).strip().lower()
    if compute not in {"cpu", "gpu"}:
        compute = "cpu"

    max_budget = cloud.get("max_budget_usd", default_budget)
    max_budget_usd = float(max_budget) if max_budget is not None else None

    effective_default_spot = default_spot if compute == "gpu" else False
    use_spot = cloud.get("use_spot", effective_default_spot)
    if isinstance(use_spot, str):
        use_spot = use_spot.strip().lower() in {"1", "true", "yes", "on"}
    else:
        use_spot = bool(use_spot)

    dataset_path_raw = cloud.get("dataset_path") or os.getenv("EXPKIT_DATASET_PATH")
    dataset_path = Path(dataset_path_raw).expanduser().resolve() if dataset_path_raw else None

    return CloudHints(
        model_size_b=model_size_b,
        dataset_size_gb=dataset_size_gb,
        speed=speed,
        max_budget_usd=max_budget_usd,
        use_spot=use_spot,
        dataset_path=dataset_path,
        compute=compute,
    )


def run_sagemaker_training(
    request: AwsTrainingRequest,
    *,
    emit_system: SYSTEM,
    emit_stdout: LINE,
    emit_stderr: LINE,
    emit_proof: PROOF,
) -> AwsTrainingResult:
    if request.script_args:
        emit_system(
            "[expkit] AWS backend currently ignores direct script CLI arguments; "
            "pass training configuration via code/env for now."
        )
    if not ROLE_ARN_RE.match(request.role_arn):
        raise RuntimeError(
            "EXPKIT_SAGEMAKER_ROLE_ARN must be an IAM role ARN "
            "(example: arn:aws:iam::<acct-id>:role/<name>)."
        )

    try:
        script_rel = request.script_path.relative_to(request.project_dir).as_posix()
    except ValueError as exc:
        raise RuntimeError("Training script must be inside project directory for AWS runs.") from exc

    session_kwargs: dict[str, Any] = {}
    if request.aws_profile:
        session_kwargs["profile_name"] = request.aws_profile

    base_session = boto3.Session(**session_kwargs)
    identity_client = base_session.client("sts")
    identity = identity_client.get_caller_identity()
    account_id = str(identity.get("Account", ""))

    if request.expected_account_id and account_id != request.expected_account_id:
        raise RuntimeError(
            "AWS account mismatch: "
            f"expected {request.expected_account_id}, got {account_id}."
        )

    emit_proof(
        "cloud_identity",
        {
            "account_id": account_id,
            "arn": identity.get("Arn"),
            "user_id": identity.get("UserId"),
        },
    )

    bucket_name = _normalize_bucket_name(request.s3_bucket)
    probe_s3 = base_session.client("s3", region_name=request.allowed_regions[0])
    bucket_region = _resolve_bucket_region(probe_s3, bucket_name)
    candidate_regions = request.allowed_regions
    if bucket_region:
        if bucket_region in request.allowed_regions:
            candidate_regions = (bucket_region,)
        else:
            candidate_regions = (bucket_region, *request.allowed_regions)
        emit_system(f"[expkit] Detected S3 bucket region: {bucket_region}")

    candidates = _ranked_instance_candidates(base_session, candidate_regions, request.hints)
    selection = candidates[0]
    emit_proof(
        "gpu_selection",
        {
            "region": selection.region,
            "instance_type": selection.instance_type,
            "hourly_usd": selection.hourly_usd,
            "estimated_hours": selection.estimated_hours,
            "estimated_cost": selection.estimated_cost,
            "gpu": selection.gpu,
            "gpu_memory_gb": selection.gpu_memory_gb,
            "speed_score": selection.speed_score,
                "inputs": {
                    "model_size_b": request.hints.model_size_b,
                    "dataset_size_gb": request.hints.dataset_size_gb,
                    "speed": request.hints.speed,
                    "compute": request.hints.compute,
                    "max_budget_usd": request.hints.max_budget_usd,
                    "use_spot": request.hints.use_spot,
                },
            "alternatives": [
                {
                    "region": c.region,
                    "instance_type": c.instance_type,
                    "estimated_cost": c.estimated_cost,
                }
                for c in candidates[:4]
            ],
        },
    )

    emit_system(
        "[expkit] AWS selection: "
        f"region={selection.region} instance={selection.instance_type} "
        f"est_cost=${selection.estimated_cost:.2f}"
    )

    region_session = boto3.Session(region_name=selection.region, **session_kwargs)
    s3_client = region_session.client("s3")

    _ensure_bucket(s3_client, bucket_name)

    run_prefix = f"expkit/runs/{request.run_id}"
    source_s3_uri = _upload_source_bundle(
        s3_client=s3_client,
        bucket=bucket_name,
        run_prefix=run_prefix,
        project_dir=request.project_dir,
        emit_system=emit_system,
    )
    input_s3_uri = _prepare_input_data(
        s3_client=s3_client,
        bucket=bucket_name,
        run_prefix=run_prefix,
        dataset_path=request.hints.dataset_path,
        emit_system=emit_system,
    )

    output_s3_uri = f"s3://{bucket_name}/{run_prefix}/output"

    env = {
        "EXPKIT_REMOTE": "1",
        "EXPKIT_RUN_ID": str(request.run_id),
        "SUPABASE_DB_URL": request.database_url,
        "EXPKIT_EXECUTION_MODE": "local",
    }

    sm_client = None
    logs_client = None
    cloudtrail_client = None
    job_name = ""
    job_arn = ""
    training_image = ""
    submit_time = None
    submit_errors: list[str] = []

    for attempt_idx, candidate in enumerate(candidates, start=1):
        region_session = boto3.Session(region_name=candidate.region, **session_kwargs)
        sm_client = region_session.client("sagemaker")
        logs_client = region_session.client("logs")
        cloudtrail_client = region_session.client("cloudtrail")
        training_image = _resolve_training_image(candidate.region, request.hints.compute)
        candidate_job_name = _job_name(request.run_id, suffix=attempt_idx if attempt_idx > 1 else None)

        hyperparameters = {
            "sagemaker_program": script_rel,
            "sagemaker_submit_directory": source_s3_uri,
            "sagemaker_container_log_level": "20",
            "sagemaker_region": candidate.region,
            "sagemaker_enable_cloudwatch_metrics": "false",
        }

        request_payload: dict[str, Any] = {
            "TrainingJobName": candidate_job_name,
            "RoleArn": request.role_arn,
            "AlgorithmSpecification": {
                "TrainingImage": training_image,
                "TrainingInputMode": "File",
            },
            "InputDataConfig": [
                {
                    "ChannelName": "training",
                    "DataSource": {
                        "S3DataSource": {
                            "S3DataType": "S3Prefix",
                            "S3Uri": input_s3_uri,
                            "S3DataDistributionType": "FullyReplicated",
                        }
                    },
                    "CompressionType": "None",
                    "InputMode": "File",
                }
            ],
            "OutputDataConfig": {
                "S3OutputPath": output_s3_uri,
            },
            "ResourceConfig": {
                "InstanceType": candidate.instance_type,
                "InstanceCount": 1,
                "VolumeSizeInGB": request.volume_size_gb,
            },
            "StoppingCondition": {
                "MaxRuntimeInSeconds": request.max_run_seconds,
            },
            "Environment": env,
            "HyperParameters": hyperparameters,
        }

        if request.hints.use_spot:
            request_payload["EnableManagedSpotTraining"] = True
            request_payload["StoppingCondition"]["MaxWaitTimeInSeconds"] = request.max_wait_seconds

        emit_system(
            f"[expkit] Submitting SageMaker training job: {candidate_job_name} "
            f"(attempt {attempt_idx}/{len(candidates)}: {candidate.instance_type} in {candidate.region})"
        )

        try:
            sm_client.create_training_job(**request_payload)
            desc = sm_client.describe_training_job(TrainingJobName=candidate_job_name)
            job_name = candidate_job_name
            job_arn = desc.get("TrainingJobArn", "")
            selection = candidate
            submit_time = datetime.now(timezone.utc)
            break
        except ClientError as exc:
            code = (exc.response.get("Error", {}) or {}).get("Code", "")
            msg = str(exc)
            submit_errors.append(f"{candidate.region}/{candidate.instance_type}: {msg}")
            if code in {"ResourceLimitExceeded", "ValidationException"}:
                emit_system(
                    f"[expkit] Candidate rejected ({candidate.instance_type} in {candidate.region}): {msg}"
                )
                continue
            raise RuntimeError(f"Failed to submit SageMaker job: {exc}") from exc
    else:
        joined = "; ".join(submit_errors[-5:]) or "No candidate could be submitted."
        raise RuntimeError(f"Failed to submit SageMaker job with available candidates: {joined}")

    if sm_client is None or logs_client is None or cloudtrail_client is None or submit_time is None:
        raise RuntimeError("Internal error: SageMaker clients were not initialized.")
    emit_proof(
        "sagemaker_job",
        {
            "job_name": job_name,
            "job_arn": job_arn,
            "region": selection.region,
            "instance_type": selection.instance_type,
            "use_spot": request.hints.use_spot,
            "input_s3_uri": input_s3_uri,
            "source_s3_uri": source_s3_uri,
            "output_s3_uri": output_s3_uri,
            "training_image": training_image,
            "submitted_at": submit_time.isoformat(),
        },
    )

    emit_system(
        "[expkit] SageMaker job submitted. "
        f"Console: https://{selection.region}.console.aws.amazon.com/sagemaker/home?region={selection.region}#/jobs/{job_name}"
    )

    final_desc, stderr_tail = _watch_job(
        sm_client=sm_client,
        logs_client=logs_client,
        job_name=job_name,
        emit_system=emit_system,
        emit_stdout=emit_stdout,
        emit_stderr=emit_stderr,
    )

    cloudtrail_proof = _lookup_cloudtrail_event(
        cloudtrail_client=cloudtrail_client,
        job_name=job_name,
        start_time=submit_time - timedelta(minutes=15),
    )
    if cloudtrail_proof:
        emit_proof("cloudtrail", cloudtrail_proof)

    status = final_desc.get("TrainingJobStatus", "Unknown")
    model_artifact_s3_uri = (
        final_desc.get("ModelArtifacts", {}) or {}
    ).get("S3ModelArtifacts")

    if status != "Completed":
        reason = final_desc.get("FailureReason") or "Training job did not complete successfully."
        suffix = f"\n--- cloud stderr tail ---\n{stderr_tail}" if stderr_tail else ""
        raise RemoteExecutionError(
            f"SageMaker job failed with status={status}: {reason}{suffix}"
        )

    emit_proof(
        "cloud_result",
        {
            "status": status,
            "training_end_time": str(final_desc.get("TrainingEndTime")),
            "billable_seconds": final_desc.get("BillableTimeInSeconds"),
            "model_artifact_s3_uri": model_artifact_s3_uri,
        },
    )

    emit_system(f"[expkit] SageMaker job completed: {job_name}")
    return AwsTrainingResult(
        job_name=job_name,
        job_arn=job_arn,
        region=selection.region,
        instance_type=selection.instance_type,
        model_artifact_s3_uri=model_artifact_s3_uri,
    )


def _resolve_training_image(region: str, compute: str) -> str:
    override = os.getenv("EXPKIT_SAGEMAKER_TRAINING_IMAGE")
    if override:
        return override

    if compute == "gpu":
        return (
            f"763104351884.dkr.ecr.{region}.amazonaws.com/"
            "pytorch-training:2.2.2-gpu-py310-cu121-ubuntu20.04-sagemaker"
        )

    return (
        f"763104351884.dkr.ecr.{region}.amazonaws.com/"
        "pytorch-training:2.1.0-cpu-py310-ubuntu20.04-sagemaker"
    )


def _upload_source_bundle(
    *,
    s3_client,
    bucket: str,
    run_prefix: str,
    project_dir: Path,
    emit_system: SYSTEM,
) -> str:
    key = f"{run_prefix}/source/source.tar.gz"
    emit_system(f"[expkit] Uploading source bundle to s3://{bucket}/{key}")

    with tempfile.TemporaryDirectory(prefix="expkit-src-") as tmp_dir:
        tar_path = Path(tmp_dir) / "source.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            for path in project_dir.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(project_dir)
                rel_text = rel.as_posix()
                if _skip_source_path(rel_text):
                    continue
                tar.add(path, arcname=rel_text)

        s3_client.upload_file(str(tar_path), bucket, key)

    return f"s3://{bucket}/{key}"


def _skip_source_path(rel_path: str) -> bool:
    blocked_prefixes = (
        ".git/",
        ".venv/",
        ".expkit/",
        "__pycache__/",
    )
    blocked_names = {
        ".env",
        ".DS_Store",
    }
    if rel_path in blocked_names:
        return True
    if any(rel_path.startswith(prefix) for prefix in blocked_prefixes):
        return True
    return "/__pycache__/" in rel_path or rel_path.endswith(".pyc")


def _ranked_instance_candidates(
    session: boto3.Session,
    regions: tuple[str, ...],
    hints: CloudHints,
) -> list[Selection]:
    speed_target = {
        "economical": 1.0,
        "balanced": 1.7,
        "fast": 2.8,
    }.get(hints.speed, 1.7)

    min_vram = _required_vram_gb(hints.model_size_b)
    catalog = CPU_CATALOG if hints.compute == "cpu" else GPU_CATALOG
    ranked: list[tuple[float, Selection]] = []

    for region in regions:
        for item in catalog:
            if hints.compute == "gpu" and item["gpu_memory_gb"] < min_vram:
                continue

            estimated_hours = _estimate_training_hours(
                model_size_b=hints.model_size_b,
                dataset_size_gb=hints.dataset_size_gb,
                speed_score=float(item["speed_score"]),
            )

            hourly = float(item["on_demand_usd"])
            if hints.use_spot:
                spot = _spot_price_usd(session=session, region=region, instance_type=item["instance_type"])
                if spot is not None:
                    hourly = spot
                else:
                    hourly = hourly * 0.45

            estimated_cost = hourly * estimated_hours

            over_budget = (
                hints.max_budget_usd is not None and estimated_cost > hints.max_budget_usd
            )
            budget_penalty = 1000.0 if over_budget else 0.0
            speed_penalty = max(0.0, speed_target - float(item["speed_score"])) * 10.0
            score = budget_penalty + speed_penalty + estimated_cost

            candidate = Selection(
                region=region,
                instance_type=str(item["instance_type"]),
                hourly_usd=hourly,
                estimated_hours=estimated_hours,
                estimated_cost=estimated_cost,
                gpu=str(item.get("gpu") or item.get("cpu")),
                gpu_memory_gb=int(item.get("gpu_memory_gb") or item.get("memory_gb") or 0),
                speed_score=float(item["speed_score"]),
            )

            ranked.append((score, candidate))

    if not ranked:
        raise RuntimeError("No GPU instance candidate matched constraints.")

    ranked.sort(key=lambda item: item[0])
    return [candidate for _, candidate in ranked]


def _required_vram_gb(model_size_b: float) -> int:
    if model_size_b <= 7:
        return 16
    if model_size_b <= 13:
        return 24
    return 40


def _estimate_training_hours(model_size_b: float, dataset_size_gb: float, speed_score: float) -> float:
    # Heuristic for POC: scales selection by data size, model size, and GPU speed.
    normalized_data = max(1.0, dataset_size_gb)
    normalized_model = max(1.0, model_size_b / 7.0)
    base = normalized_data / max(0.7, speed_score * 1.8)
    return max(0.25, base * normalized_model)


def _spot_price_usd(session: boto3.Session, region: str, instance_type: str) -> float | None:
    ec2_instance = instance_type.removeprefix("ml.")
    try:
        ec2 = session.client("ec2", region_name=region)
        resp = ec2.describe_spot_price_history(
            InstanceTypes=[ec2_instance],
            ProductDescriptions=["Linux/UNIX"],
            StartTime=datetime.now(timezone.utc),
            MaxResults=1,
        )
        history = resp.get("SpotPriceHistory") or []
        if not history:
            return None
        return float(history[0]["SpotPrice"])
    except Exception:
        return None


def _ensure_bucket(s3_client, bucket: str) -> None:
    try:
        s3_client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        raise RuntimeError(f"Cannot access S3 bucket {bucket!r}: {exc}") from exc


def _resolve_bucket_region(s3_client, bucket: str) -> str | None:
    try:
        resp = s3_client.get_bucket_location(Bucket=bucket)
    except ClientError:
        return None

    loc = resp.get("LocationConstraint")
    if not loc:
        return "us-east-1"
    return str(loc)


def _normalize_bucket_name(bucket: str) -> str:
    value = bucket.strip()
    arn_prefix = "arn:aws:s3:::"
    if value.startswith(arn_prefix):
        value = value[len(arn_prefix) :]
    return value.strip("/")


def _prepare_input_data(
    *,
    s3_client,
    bucket: str,
    run_prefix: str,
    dataset_path: Path | None,
    emit_system: SYSTEM,
) -> str:
    if dataset_path and not dataset_path.exists():
        raise RuntimeError(f"Dataset path does not exist: {dataset_path}")

    channel_prefix = f"{run_prefix}/input/training"

    if not dataset_path:
        key = f"{channel_prefix}/placeholder.txt"
        s3_client.put_object(Bucket=bucket, Key=key, Body=b"expkit")
        return f"s3://{bucket}/{channel_prefix}/"

    if dataset_path.is_file():
        key = f"{channel_prefix}/{dataset_path.name}"
        emit_system(f"[expkit] Uploading dataset file to s3://{bucket}/{key}")
        s3_client.upload_file(str(dataset_path), bucket, key)
        return f"s3://{bucket}/{channel_prefix}/"

    emit_system(f"[expkit] Uploading dataset directory to s3://{bucket}/{channel_prefix}/")
    for file_path in dataset_path.rglob("*"):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(dataset_path)
        key = f"{channel_prefix}/{rel.as_posix()}"
        s3_client.upload_file(str(file_path), bucket, key)

    return f"s3://{bucket}/{channel_prefix}/"


def _job_name(run_id: UUID, suffix: int | None = None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    base = f"expkit-{str(run_id)[:8]}-{stamp}"
    if suffix is not None:
        base = f"{base}-{suffix}"
    return base[:63]


def _watch_job(
    *,
    sm_client,
    logs_client,
    job_name: str,
    emit_system: SYSTEM,
    emit_stdout: LINE,
    emit_stderr: LINE,
) -> tuple[dict[str, Any], str]:
    tokens: dict[str, str | None] = {}
    last_status: str | None = None
    stderr_tail: deque[str] = deque(maxlen=80)

    while True:
        desc = sm_client.describe_training_job(TrainingJobName=job_name)
        status = desc.get("TrainingJobStatus", "Unknown")
        secondary = desc.get("SecondaryStatus", "")

        if status != last_status:
            emit_system(f"[expkit] SageMaker status changed: {status} ({secondary})")
            last_status = status

        _drain_logs(
            logs_client=logs_client,
            job_name=job_name,
            tokens=tokens,
            emit_stdout=emit_stdout,
            emit_stderr=emit_stderr,
            stderr_tail=stderr_tail,
        )

        if status in {"Completed", "Failed", "Stopped"}:
            _drain_logs(
                logs_client=logs_client,
                job_name=job_name,
                tokens=tokens,
                emit_stdout=emit_stdout,
                emit_stderr=emit_stderr,
                stderr_tail=stderr_tail,
            )
            return desc, "\n".join(stderr_tail)

        time.sleep(8)


def _drain_logs(
    *,
    logs_client,
    job_name: str,
    tokens: dict[str, str | None],
    emit_stdout: LINE,
    emit_stderr: LINE,
    stderr_tail: deque[str],
) -> None:
    try:
        streams = logs_client.describe_log_streams(
            logGroupName="/aws/sagemaker/TrainingJobs",
            logStreamNamePrefix=job_name,
            orderBy="LogStreamName",
            descending=False,
        ).get("logStreams", [])
    except (ClientError, BotoCoreError):
        return

    for stream in streams:
        name = stream.get("logStreamName")
        if not name:
            continue

        token = tokens.get(name)
        for _ in range(12):
            kwargs: dict[str, Any] = {
                "logGroupName": "/aws/sagemaker/TrainingJobs",
                "logStreamName": name,
                "startFromHead": token is None,
            }
            if token:
                kwargs["nextToken"] = token

            try:
                resp = logs_client.get_log_events(**kwargs)
            except (ClientError, BotoCoreError):
                break

            events = resp.get("events", [])
            for event in events:
                msg = str(event.get("message", "")).rstrip("\n")
                if not msg:
                    continue
                if _looks_like_error(msg):
                    emit_stderr(msg)
                    stderr_tail.append(msg)
                else:
                    emit_stdout(msg)

            next_token = resp.get("nextForwardToken")
            if not next_token or next_token == token:
                tokens[name] = next_token or token
                break
            token = next_token
            tokens[name] = token


def _looks_like_error(message: str) -> bool:
    return bool(re.search(r"\b(ERROR|Error|Exception|Traceback|RuntimeError|ValueError)\b", message))


def _lookup_cloudtrail_event(
    *,
    cloudtrail_client,
    job_name: str,
    start_time: datetime,
) -> dict[str, Any] | None:
    try:
        resp = cloudtrail_client.lookup_events(
            LookupAttributes=[
                {
                    "AttributeKey": "EventName",
                    "AttributeValue": "CreateTrainingJob",
                }
            ],
            StartTime=start_time,
            EndTime=datetime.now(timezone.utc) + timedelta(minutes=10),
            MaxResults=50,
        )
    except (ClientError, BotoCoreError):
        return None

    for event in resp.get("Events", []):
        raw = event.get("CloudTrailEvent")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue

        params = payload.get("requestParameters", {})
        if params.get("trainingJobName") != job_name:
            continue

        return {
            "event_id": event.get("EventId"),
            "event_time": str(event.get("EventTime")),
            "event_name": event.get("EventName"),
            "username": event.get("Username"),
            "event_source": event.get("EventSource"),
        }

    return None
