"""Asynchronous engine and request-scoped transaction dependency."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config.config import settings

database_url = str(settings.get("db.url"))
engine_options: dict[str, object] = {"pool_pre_ping": True}
if not database_url.startswith("sqlite"):
    engine_options["pool_size"] = int(settings.get("db.pool_size", 10))
    engine_options["pool_timeout"] = int(settings.get("db.timeout_seconds", 10))

async_engine = create_async_engine(database_url, **engine_options)
async_session_factory = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
