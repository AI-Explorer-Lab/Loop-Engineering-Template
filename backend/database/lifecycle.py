"""Database readiness, controlled table creation, and shutdown."""

from sqlalchemy import text

from config.config import settings
from database.models import Base
from database.session import async_engine


async def init_database() -> None:
    async with async_engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def check_database() -> bool:
    try:
        await init_database()
    except Exception:
        return False
    return True


async def close_database() -> None:
    await async_engine.dispose()


async def create_tables() -> None:
    environment = str(settings.get("environment.name", ""))
    if environment not in {"development", "test", "local"}:
        raise RuntimeError("create_tables() is disabled outside local and test environments")
    async with async_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
