from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import Settings, load_settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_async_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine
    current_settings = settings or load_settings()
    if _engine is None:
        logger.info("Creating async database engine for %s", current_settings.paths.database_path)
        _engine = create_async_engine(current_settings.database_url, future=True)
    return _engine


def get_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        engine = get_async_engine(settings)
        _session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        logger.debug("Created async session factory")
    return _session_factory


@asynccontextmanager
async def session_scope(settings: Settings | None = None):
    factory = get_session_factory(settings)
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        logger.exception("Rolling back database transaction")
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        logger.info("Disposing async database engine")
        await _engine.dispose()
        _engine = None
        _session_factory = None

