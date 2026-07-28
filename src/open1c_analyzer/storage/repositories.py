"""Repository abstractions for storage operations."""

from collections.abc import Collection

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from open1c_analyzer.storage.models import Project, SourceFile


class ProjectRepository:
    """Persist and query projects."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, name: str, source_path: str | None = None) -> Project:
        """Create a project."""
        project = Project(name=name, source_path=source_path)
        self._session.add(project)
        self._session.flush()
        return project

    def get_by_name(self, name: str) -> Project | None:
        """Return a project by its exact name."""
        statement = select(Project).where(Project.name == name)
        return self._session.scalar(statement)

    def get_by_source_path(self, source_path: str) -> Project | None:
        """Return a project by its normalized source path."""
        statement = select(Project).where(Project.source_path == source_path)
        return self._session.scalar(statement)

    def list_all(self) -> list[Project]:
        """Return all projects ordered by name."""
        statement = select(Project).order_by(Project.name)
        return list(self._session.scalars(statement))

    def remove(self, project: Project) -> None:
        """Delete a project and its catalogued files."""
        self._session.delete(project)
        self._session.flush()


class SourceFileRepository:
    """Persist and query catalogued source files."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        *,
        project_id: int,
        relative_path: str,
        checksum: str,
        language: str,
        size_bytes: int,
        modified_ns: int,
    ) -> SourceFile:
        """Create a source-file record."""
        source_file = SourceFile(
            project_id=project_id,
            relative_path=relative_path,
            checksum=checksum,
            language=language,
            size_bytes=size_bytes,
            modified_ns=modified_ns,
        )
        self._session.add(source_file)
        self._session.flush()
        return source_file

    def list_for_project(self, project_id: int) -> list[SourceFile]:
        """Return all files for a project ordered by relative path."""
        statement = (
            select(SourceFile)
            .where(SourceFile.project_id == project_id)
            .order_by(SourceFile.relative_path)
        )
        return list(self._session.scalars(statement))

    def count_for_project(self, project_id: int) -> int:
        """Return the number of catalogued files for a project."""
        statement = select(func.count(SourceFile.id)).where(SourceFile.project_id == project_id)
        return int(self._session.scalar(statement) or 0)

    def remove_not_in(self, project_id: int, relative_paths: Collection[str]) -> int:
        """Delete files that are no longer present in the source directory."""
        statement = delete(SourceFile).where(SourceFile.project_id == project_id)
        if relative_paths:
            statement = statement.where(SourceFile.relative_path.not_in(relative_paths))
        result = self._session.execute(statement)
        self._session.flush()
        return int(getattr(result, "rowcount", 0) or 0)
