# ExpKit POC

ExpKit is a proof-of-concept Python SDK for running ML experiments with a remote-style execution flow, typed logging, and a live localhost dashboard.

## What this POC does

- Provides `trainer.run()` so users write normal Python training code.
- Provides `trainer.log()` for structured metric/event logging.
- Starts a localhost dashboard immediately at run startup.
- Auto-generates a Dockerfile and attempts Docker execution first.
- Falls back to isolated local worker process if Docker daemon is unavailable.
- Persists runs/events into Supabase Postgres.
- Starts a polished dashboard at `http://127.0.0.1:<port>/runs/<run_id>` with live metrics and logs.
- Propagates remote worker failures back to terminal with stderr tail.

## Quick start

1. Create a Supabase Postgres database and set env var:

```bash
export SUPABASE_DB_URL='postgresql://postgres:[password]@[host]:5432/postgres'
```

2. Install dependencies:

```bash
pip install -e .
```

3. Run the example:

```bash
python examples/train_example.py
```

The SDK prints the run ID and dashboard URL.

## User API

```python
from expkit import trainer


def train():
    for step in range(100):
        loss = ...
        trainer.log("loss", loss, step=step)


if __name__ == "__main__":
    trainer.run(train)
```

## Event typing

`trainer.log(..., kind="metric")` stores data in `events.event_type`. Current event types emitted by the SDK:

- `metric`: explicit `trainer.log` calls
- `stdout`: worker stdout line
- `stderr`: worker stderr line / traceback

Schema is in [`db/schema.sql`](db/schema.sql).

## Notes

- For this POC, cloud allocation is simulated by running in Docker or a separate worker process.
- Auth, multi-user isolation, artifact storage, and run comparison are intentionally out of scope.
