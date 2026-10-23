"""Database infrastructure tests."""

from pathlib import Path

from sqlalchemy import text

from open1c_analyzer.storage.database import Database


def test_database_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "catalog.db"
    database = Database(f"sqlite:///{path.as_posix()}")

    with database.session() as session:
        result = session.scalar(text("SELECT 1"))

    database.dispose()

    assert result == 1
    assert path.exists()


def test_wal_reader_keeps_snapshot_while_another_connection_commits(tmp_path: Path) -> None:
    path = tmp_path / "snapshots.db"
    database = Database(f"sqlite:///{path.as_posix()}")
    with database.session() as session:
        session.execute(text("CREATE TABLE example (value INTEGER)"))
        session.execute(text("INSERT INTO example VALUES (1)"))
    with database.session() as reader:
        assert reader.scalar(text("PRAGMA journal_mode")) == "wal"
        assert reader.scalar(text("SELECT value FROM example")) == 1
        with database.session() as writer:
            writer.execute(text("UPDATE example SET value=2"))
        assert reader.scalar(text("SELECT value FROM example")) == 1
    with database.session() as reader:
        assert reader.scalar(text("SELECT value FROM example")) == 2
        assert reader.scalar(text("PRAGMA foreign_keys")) == 1
    database.dispose()
