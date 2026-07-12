# Database migrations

Alembic migration files record executable database structure changes. Create a
revision from `backend/` with:

```bash
alembic revision --autogenerate -m "describe the change"
```

Review generated SQL before applying it. Apply pending migrations with:

```bash
alembic upgrade head
```

Production never calls `create_tables()`; it only runs reviewed migrations.
