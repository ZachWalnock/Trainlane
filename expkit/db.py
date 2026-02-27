from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row


@dataclass(slots=True)
class Store:
    database_url: str

    def _connect(self):
        return psycopg.connect(self.database_url)

    def init_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists runs (
                        id uuid primary key,
                        status text not null,
                        script_path text not null,
                        backend text not null,
                        image_tag text,
                        started_at timestamptz not null,
                        ended_at timestamptz,
                        error_summary text,
                        metadata jsonb not null default '{}'::jsonb
                    )
                    """
                )
                cur.execute(
                    """
                    create table if not exists events (
                        id bigserial primary key,
                        run_id uuid not null references runs(id) on delete cascade,
                        ts timestamptz not null,
                        event_type text not null,
                        key text,
                        value_json jsonb not null,
                        step bigint,
                        source text not null default 'sdk'
                    )
                    """
                )
                cur.execute(
                    """
                    create index if not exists events_run_id_id_idx on events(run_id, id)
                    """
                )
            conn.commit()

    def create_run(
        self,
        *,
        run_id: UUID,
        script_path: str,
        backend: str,
        image_tag: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into runs (id, status, script_path, backend, image_tag, started_at, metadata)
                    values (%s, 'running', %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        script_path,
                        backend,
                        image_tag,
                        datetime.now(timezone.utc),
                        json.dumps(metadata or {}),
                    ),
                )
            conn.commit()

    def append_event(
        self,
        *,
        run_id: UUID,
        event_type: str,
        key: str | None,
        value: dict[str, Any],
        step: int | None,
        source: str = "sdk",
    ) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into events (run_id, ts, event_type, key, value_json, step, source)
                    values (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        datetime.now(timezone.utc),
                        event_type,
                        key,
                        json.dumps(value),
                        step,
                        source,
                    ),
                )
            conn.commit()

    def finish_run(
        self, *, run_id: UUID, status: str, error_summary: str | None = None
    ) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update runs
                    set status = %s,
                        error_summary = %s,
                        ended_at = %s
                    where id = %s
                    """,
                    (status, error_summary, datetime.now(timezone.utc), run_id),
                )
            conn.commit()

    def update_run_execution(
        self, *, run_id: UUID, backend: str, image_tag: str | None = None
    ) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update runs
                    set backend = %s,
                        image_tag = %s
                    where id = %s
                    """,
                    (backend, image_tag, run_id),
                )
            conn.commit()

    def get_run(self, run_id: UUID) -> dict[str, Any] | None:
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    select id, status, script_path, backend, image_tag, started_at, ended_at, error_summary
                    from runs
                    where id = %s
                    """,
                    (run_id,),
                )
                return cur.fetchone()

    def get_events(self, run_id: UUID, after_id: int = 0, limit: int = 300) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    select id, ts, event_type, key, value_json, step, source
                    from events
                    where run_id = %s and id > %s
                    order by id asc
                    limit %s
                    """,
                    (run_id, after_id, limit),
                )
                rows = cur.fetchall()
                return rows
