# Backend

The backend is a phase-one FastAPI scaffold following the repository's
Controller → Service → Domain/Mapper boundaries.

## Local setup

```bash
conda activate loop-engineering
python -m pip install -e 'backend[dev]'
cd backend
LOOP_ENV=test pytest
LOOP_ENV=development uvicorn main:app --reload
```

The development environment expects PostgreSQL at the URL in
`config/app.yaml`. Override it with `LOOP_DB__URL`; never commit credentials.

## Endpoints

- `GET /health/live`: process liveness.
- `GET /health/ready`: database readiness.
- `GET /docs`: generated OpenAPI UI.

## Database changes

Use Alembic migrations for shared and production databases. `create_tables()`
is restricted to local and test environments and is not a deployment strategy.
