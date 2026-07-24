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
