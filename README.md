# ExpKit POC

ExpKit is a proof-of-concept Python SDK for running ML experiments with a remote-style execution flow, typed logging, and a live localhost dashboard.

## What this POC does

- Provides `trainer.run()` so users write normal Python training code.
- Provides `trainer.log()` for structured metric/event logging.
- Starts a localhost dashboard immediately at run startup.
- Supports two execution modes:
  - `local`: Docker/process-backed simulated remote execution.
  - `aws`: real SageMaker training jobs in your AWS account.
- Preserves local fallback by switching `EXPKIT_EXECUTION_MODE=local`.
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

## Run locally (no cloud)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

export SUPABASE_DB_URL='postgresql://postgres:[password]@[host]:5432/postgres'
export EXPKIT_EXECUTION_MODE=local
export EXPKIT_DOCKER=0

python examples/train_example.py
```

Use `EXPKIT_DOCKER=1` to use Docker-backed local simulation instead of direct local worker execution.

## Install from PyPI

```bash
pip install expkit
```

## AWS mode (real cloud compute)

Set:

```bash
export EXPKIT_EXECUTION_MODE=aws
export AWS_PROFILE=expkit
export AWS_ACCOUNT_ID=123456789012
export EXPKIT_AWS_REGIONS=us-east-1,us-east-2
export EXPKIT_SAGEMAKER_ROLE_ARN=arn:aws:iam::123456789012:role/expkit-sagemaker-exec
export EXPKIT_AWS_S3_BUCKET=your-expkit-bucket
export EXPKIT_MAX_BUDGET_USD_PER_RUN=50
export EXPKIT_AWS_COMPUTE=cpu
export EXPKIT_AWS_USE_SPOT=0
```

Then run:

```bash
python examples/train_cloud_example.py
```

ExpKit will:

- Select a small SageMaker instance using model size, dataset size, speed preference, and budget.
- Submit a real SageMaker training job.
- Stream CloudWatch logs back into terminal + dashboard.
- Persist cloud proof artifacts (`account identity`, `job ARN`, `CloudTrail event`) in `events` as `event_type='proof'`.

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
- `system`: orchestration status messages
- `proof`: cloud-account/job/audit artifacts for verification

Schema is in [`db/schema.sql`](db/schema.sql).

## Notes

- `aws` mode uses SageMaker in your AWS account. `local` mode keeps the original POC path.
- Auth, multi-user isolation, artifact storage, and run comparison are intentionally out of scope.

## Publish to PyPI

### One-time setup

1. Create `expkit` on PyPI and TestPyPI.
2. In GitHub repo settings, configure Trusted Publisher for:
   - TestPyPI environment: `testpypi`
   - PyPI environment: `pypi`

### Local release checks

```bash
python -m pip install -U build twine
python -m build
twine check dist/*
```

### Release

Push a version tag:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The GitHub Actions workflow at `.github/workflows/pypi-publish.yml` publishes to TestPyPI first, then PyPI.
