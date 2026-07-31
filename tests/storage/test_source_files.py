"""Source-file repository tests."""

from sqlalchemy import create_engine, event
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


def test_remove_not_in_does_not_expand_large_path_set_into_sql() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    delete_parameter_counts: list[int] = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture_delete_parameter_count(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("DELETE"):
            delete_parameter_counts.append(len(parameters))  # type: ignore[arg-type]

    with Session(engine) as session:
        project = ProjectRepository(session).add("Large", "/projects/large")
        files = SourceFileRepository(session)
        files.add(
            project_id=project.id,
            relative_path="keep.bsl",
            checksum="a" * 64,
            language="bsl",
            size_bytes=12,
            modified_ns=100,
        )
        files.add(
            project_id=project.id,
            relative_path="remove.bsl",
            checksum="b" * 64,
            language="bsl",
            size_bytes=12,
            modified_ns=100,
        )

        retained_paths = {"keep.bsl", *(f"Modules/Module{index}.bsl" for index in range(40_000))}

        assert files.remove_not_in(project.id, retained_paths) == 1
        assert [item.relative_path for item in files.list_for_project(project.id)] == ["keep.bsl"]
        assert delete_parameter_counts
        assert max(delete_parameter_counts) <= 500

    engine.dispose()
