"""Shared pytest fixtures."""

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def database_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Configure an isolated application database."""
    path = tmp_path / "open1c.db"
    monkeypatch.setenv("OPEN1C_DATABASE_PATH", str(path))
    yield path
