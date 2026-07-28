"""Project repository tests."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from open1c_analyzer.storage.models import Base
from open1c_analyzer.storage.repositories import ProjectRepository


def test_project_repository_round_trip() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        repository = ProjectRepository(session)
        created = repository.add("TMS", "/projects/tms")
        session.commit()

        found = repository.get_by_name("TMS")
        found_by_path = repository.get_by_source_path("/projects/tms")

    engine.dispose()

    assert found is not None
    assert found.id == created.id
    assert found.uuid
    assert found_by_path is not None
    assert found_by_path.id == created.id
