from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(slots=True)
class Settings:
    database_url: str
    host: str = "127.0.0.1"
    dashboard_port: int = 8421
    docker_enabled: bool = True

    @property
    def project_root(self) -> Path:
        return Path.cwd()



def load_settings() -> Settings:
    database_url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "Set SUPABASE_DB_URL (or DATABASE_URL) to your Supabase Postgres connection string."
        )

    dashboard_port = int(os.getenv("EXPKIT_DASHBOARD_PORT", "8421"))
    docker_enabled = os.getenv("EXPKIT_DOCKER", "1") != "0"

    return Settings(
        database_url=database_url,
        dashboard_port=dashboard_port,
        docker_enabled=docker_enabled,
    )
