"""Repository abstractions for storage operations."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from open1c_analyzer.storage.models import Project


class ProjectRepository:
    """Persist and query projects."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, name: str) -> Project:
        """Create a project."""
        project = Project(name=name)
        self._session.add(project)
        self._session.flush()
        return project

    def get_by_name(self, name: str) -> Project | None:
        """Return a project by its exact name."""
        statement = select(Project).where(Project.name == name)
        return self._session.scalar(statement)

    def list_all(self) -> list[Project]:
        """Return all projects ordered by name."""
        statement = select(Project).order_by(Project.name)
        return list(self._session.scalars(statement))
