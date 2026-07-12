# Loop Engineering Example

This repository is the shared scaffold for a Loop Engineering workflow: a
human defines the goal and boundaries once, then an AI plans, implements,
verifies, and corrects its work until it reaches a proven result or a declared
stop condition.

## Start here

1. Copy [`template/initial-loop-request.md`](template/initial-loop-request.md).
2. Fill in the human input section.
3. Give the completed file to the AI as the task specification.
4. The AI follows `AGENTS.md`, works inside the granted authority, and returns
   only when complete or genuinely blocked.

## Current implementation stage

The repository currently contains the phase-one runnable scaffold:

- FastAPI backend with configuration, health endpoints, error handling,
  request IDs, async database sessions, and Alembic;
- React/TypeScript frontend shell;
- PostgreSQL local environment through Docker Compose;
- backend tests and a basic CI workflow.

Agent execution, Knowledge-Base integration, and external platform adapters are
added in later phases when their first real use case is selected.

## Run with Docker

```bash
cp .env.example .env
docker compose up --build
```

- API: <http://localhost:8000>
- API documentation: <http://localhost:8000/docs>
- Liveness: <http://localhost:8000/health/live>
- Readiness: <http://localhost:8000/health/ready>

## Run backend tests locally

```bash
conda activate loop-engineering
make backend-test
```

The project environment uses Anaconda with Python 3.12. Recreate it on another
machine with `conda env create -f environment.yml`; update an existing copy with
`make environment-update`.

See `backend/README.md` for direct backend commands.
