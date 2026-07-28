"""Source-file repository tests."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from open1c_analyzer.storage.models import Base
from open1c_analyzer.storage.repositories import ProjectRepository, SourceFileRepository


def test_source_file_repository_add_count_and_remove() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        project = ProjectRepository(session).add("Demo", "/projects/demo")
        files = SourceFileRepository(session)
        files.add(
            project_id=project.id,
            relative_path="CommonModules/Demo/Module.bsl",
            checksum="a" * 64,
            language="bsl",
            size_bytes=12,
            modified_ns=100,
        )
        files.add(
            project_id=project.id,
            relative_path="Configuration.xml",
            checksum="b" * 64,
            language="xml",
            size_bytes=20,
            modified_ns=200,
        )

        assert files.count_for_project(project.id) == 2
        assert files.remove_not_in(project.id, {"Configuration.xml"}) == 1
        assert [item.relative_path for item in files.list_for_project(project.id)] == [
            "Configuration.xml"
        ]

    engine.dispose()
