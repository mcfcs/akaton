from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from akaton.persistence.models import Base


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        if url.startswith("sqlite") and "///" in url:
            path_text = url.split("///", 1)[1]
            if path_text and path_text != ":memory:":
                Path(path_text).parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(url, echo=echo)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def close(self) -> None:
        await self.engine.dispose()


def upgrade_database(url: str, root: Path) -> None:
    """Run Alembic with a synchronous driver outside the async event loop."""
    sync_url = url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", sync_url.replace("%", "%%"))
    command.upgrade(config, "head")
