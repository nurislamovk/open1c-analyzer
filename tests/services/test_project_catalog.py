"""Project catalog service tests."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from open1c_analyzer.services.project_catalog import (
    ProjectAlreadyExistsError,
    ProjectCatalog,
    SourceDirectoryError,
)
from open1c_analyzer.storage.models import Base


@contextmanager
def _session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def test_add_project_normalizes_path_and_rejects_duplicates(tmp_path: Path) -> None:
    with _session() as session:
        catalog = ProjectCatalog(session)
        project = catalog.add_project("Demo", tmp_path)

        assert project.source_path == str(tmp_path.resolve())
        with pytest.raises(ProjectAlreadyExistsError):
            catalog.add_project("Demo", tmp_path / "other")
        with pytest.raises(ProjectAlreadyExistsError):
            catalog.add_project("Other", tmp_path)


def test_add_project_rejects_missing_directory(tmp_path: Path) -> None:
    with _session() as session, pytest.raises(SourceDirectoryError):
        ProjectCatalog(session).add_project("Missing", tmp_path / "missing")


def test_scan_is_idempotent_and_tracks_changes(tmp_path: Path) -> None:
    module = tmp_path / "CommonModules" / "Demo" / "Module.bsl"
    module.parent.mkdir(parents=True)
    module.write_text("procedure Test()", encoding="utf-8")
    metadata = tmp_path / "Configuration.xml"
    metadata.write_text("<Configuration />", encoding="utf-8")

    ignored = tmp_path / ".git" / "config"
    ignored.parent.mkdir()
    ignored.write_text("ignored", encoding="utf-8")

    with _session() as session:
        catalog = ProjectCatalog(session)
        project = catalog.add_project("Demo", tmp_path)

        first = catalog.scan_project("Demo")
        assert (first.added, first.updated, first.unchanged, first.removed) == (2, 0, 0, 0)
        assert first.total == 2

        second = catalog.scan_project("Demo")
        assert (second.added, second.updated, second.unchanged, second.removed) == (0, 0, 2, 0)

        module.write_text("procedure Changed()", encoding="utf-8")
        metadata.unlink()
        new_file = tmp_path / "Language.json"
        new_file.write_text('{"lang": "ru"}', encoding="utf-8")

        third = catalog.scan_project("Demo")
        assert (third.added, third.updated, third.unchanged, third.removed) == (1, 1, 0, 1)

        files = catalog.list_files(project)
        assert [item.relative_path for item in files] == [
            "CommonModules/Demo/Module.bsl",
            "Language.json",
        ]
        assert [item.language for item in files] == ["bsl", "json"]


def test_remove_project_deletes_catalog(tmp_path: Path) -> None:
    source = tmp_path / "Module.bsl"
    source.write_text("", encoding="utf-8")

    with _session() as session:
        catalog = ProjectCatalog(session)
        catalog.add_project("Demo", tmp_path)
        catalog.scan_project("Demo")
        catalog.remove_project("Demo")

        assert catalog.list_projects() == []
