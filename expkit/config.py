from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv


load_dotenv()


@dataclass(slots=True)
class Settings:
    database_url: str
    host: str = "127.0.0.1"
    dashboard_port: int = 8421
    docker_enabled: bool = True
    execution_mode: str = "local"
    aws_profile: str | None = None
    aws_account_id: str | None = None
    aws_regions: tuple[str, ...] = ("us-east-1", "us-east-2")
    aws_s3_bucket: str | None = None
    aws_sagemaker_role_arn: str | None = None
    aws_ecr_repo_uri: str | None = None
    max_budget_usd_per_run: float | None = None
    aws_use_spot: bool = False

    @property
    def project_root(self) -> Path:
        return Path.cwd()


def _parse_csv(value: str | None, default: Sequence[str]) -> tuple[str, ...]:
    if not value:
        return tuple(default)
    items = [item.strip() for item in value.split(",") if item.strip()]
    return tuple(items or default)


def _parse_optional_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid float value: {value!r}") from exc


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default



def load_settings() -> Settings:
    database_url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "Set SUPABASE_DB_URL (or DATABASE_URL) to your Supabase Postgres connection string."
        )

    dashboard_port = int(os.getenv("EXPKIT_DASHBOARD_PORT", "8421"))
    docker_enabled = os.getenv("EXPKIT_DOCKER", "1") != "0"
    execution_mode = os.getenv("EXPKIT_EXECUTION_MODE", "local").strip().lower()
    if execution_mode not in {"local", "aws"}:
        raise RuntimeError(
            "EXPKIT_EXECUTION_MODE must be either 'local' or 'aws'."
        )

    aws_regions = _parse_csv(os.getenv("EXPKIT_AWS_REGIONS"), ("us-east-1", "us-east-2"))
    aws_profile = os.getenv("AWS_PROFILE") or os.getenv("EXPKIT_AWS_PROFILE")
    aws_account_id = os.getenv("AWS_ACCOUNT_ID")
    aws_s3_bucket = os.getenv("EXPKIT_AWS_S3_BUCKET")
    aws_sagemaker_role_arn = os.getenv("EXPKIT_SAGEMAKER_ROLE_ARN")
    aws_ecr_repo_uri = os.getenv("EXPKIT_AWS_ECR_REPO_URI")
    max_budget_usd_per_run = _parse_optional_float(os.getenv("EXPKIT_MAX_BUDGET_USD_PER_RUN"))
    aws_use_spot = _parse_bool(os.getenv("EXPKIT_AWS_USE_SPOT"), False)

    return Settings(
        database_url=database_url,
        dashboard_port=dashboard_port,
        docker_enabled=docker_enabled,
        execution_mode=execution_mode,
        aws_profile=aws_profile,
        aws_account_id=aws_account_id,
        aws_regions=aws_regions,
        aws_s3_bucket=aws_s3_bucket,
        aws_sagemaker_role_arn=aws_sagemaker_role_arn,
        aws_ecr_repo_uri=aws_ecr_repo_uri,
        max_budget_usd_per_run=max_budget_usd_per_run,
        aws_use_spot=aws_use_spot,
    )
