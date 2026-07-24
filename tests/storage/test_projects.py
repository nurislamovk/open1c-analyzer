"""Project repository tests."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from open1c_analyzer.storage.models import Base
from open1c_analyzer.storage.repositories import ProjectRepository


def test_add_and_get_project() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        repository = ProjectRepository(session)
        created = repository.add("TMS")
        session.commit()

        found = repository.get_by_name("TMS")

    assert found is not None
    assert found.id == created.id
    assert found.uuid
    engine.dispose()
