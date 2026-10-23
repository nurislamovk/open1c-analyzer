"""Database engine and session management."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, event
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
            # Let SQLAlchemy issue BEGIN even for SELECT/savepoints. sqlite3's
            # legacy transaction mode can otherwise commit a standalone SAVEPOINT.
            dbapi_connection.isolation_level = None  # type: ignore[attr-defined]
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        @event.listens_for(self.engine, "begin")
        def begin_sqlite_transaction(connection: Connection) -> None:
            connection.exec_driver_sql("BEGIN")

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
