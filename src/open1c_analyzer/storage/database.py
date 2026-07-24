"""Database engine and session management."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


class Database:
    """Own the SQLite engine and create transactional sessions."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._ensure_sqlite_directory()
        self.engine: Engine = create_engine(database_url, future=True)
        self._configure_sqlite()
        self._session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
        )

    def _ensure_sqlite_directory(self) -> None:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            return
        database_name = self.database_url.removeprefix(prefix)
        if database_name == ":memory:":
            return
        Path(database_name).expanduser().parent.mkdir(parents=True, exist_ok=True)

    def _configure_sqlite(self) -> None:
        if not self.database_url.startswith("sqlite"):
            return

        @event.listens_for(self.engine, "connect")
        def set_sqlite_pragma(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield a session and commit or roll back the transaction."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        """Close pooled database connections."""
        self.engine.dispose()
