"""Project catalog and source-directory synchronization."""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from sqlalchemy.orm import Session

from open1c_analyzer.storage.models import Project, SourceFile
from open1c_analyzer.storage.repositories import ProjectRepository, SourceFileRepository

_IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".hg",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
}

_LANGUAGE_BY_SUFFIX = {
    ".bsl": "bsl",
    ".json": "json",
    ".mdo": "mdo",
    ".os": "bsl",
    ".toml": "toml",
    ".txt": "text",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}


class ProjectCatalogError(Exception):
    """Base error for project-catalog operations."""


class ProjectNotFoundError(ProjectCatalogError):
    """Raised when a requested project is absent."""


class ProjectAlreadyExistsError(ProjectCatalogError):
    """Raised when a project name or source path is already registered."""


class SourceDirectoryError(ProjectCatalogError):
    """Raised when a source directory is invalid or unavailable."""


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Summary of one source-directory synchronization."""

    added: int
    updated: int
    unchanged: int
    removed: int

    @property
    def total(self) -> int:
        """Return the number of files present after synchronization."""
        return self.added + self.updated + self.unchanged


class ProjectCatalog:
    """Manage registered projects and their file catalogs."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._projects = ProjectRepository(session)
        self._files = SourceFileRepository(session)

    def add_project(self, name: str, source_path: Path) -> Project:
        """Register a source directory as a named project."""
        clean_name = name.strip()
        if not clean_name:
            raise ProjectCatalogError("Project name must not be empty.")

        if self._projects.get_by_name(clean_name) is not None:
            raise ProjectAlreadyExistsError(f"Project already exists: {clean_name}")

        normalized_path = self._normalize_directory(source_path)
        normalized_text = str(normalized_path)
        if self._projects.get_by_source_path(normalized_text) is not None:
            raise ProjectAlreadyExistsError(
                f"Source directory is already registered: {normalized_text}"
            )

        return self._projects.add(clean_name, normalized_text)

    def get_project(self, name: str) -> Project:
        """Return a project or raise a domain error."""
        project = self._projects.get_by_name(name)
        if project is None:
            raise ProjectNotFoundError(f"Project not found: {name}")
        return project

    def list_projects(self) -> list[Project]:
        """Return all registered projects."""
        return self._projects.list_all()

    def remove_project(self, name: str) -> None:
        """Remove a project and its catalogued files."""
        project = self.get_project(name)
        self._projects.remove(project)

    def file_count(self, project: Project) -> int:
        """Return the number of files catalogued for a project."""
        return self._files.count_for_project(project.id)

    def list_files(self, project: Project) -> list[SourceFile]:
        """Return catalogued files for a project."""
        return self._files.list_for_project(project.id)

    def scan_project(self, name: str) -> ScanResult:
        """Synchronize the database file catalog with the source directory."""
        project = self.get_project(name)
        if project.source_path is None:
            raise SourceDirectoryError(f"Project has no source directory: {name}")

        root = self._normalize_directory(Path(project.source_path))
        existing = {
            source_file.relative_path: source_file
            for source_file in self._files.list_for_project(project.id)
        }
        seen: set[str] = set()
        added = 0
        updated = 0
        unchanged = 0

        for path in self._iter_source_files(root):
            relative_path = path.relative_to(root).as_posix()
            seen.add(relative_path)
            stat = path.stat()
            checksum = self._checksum(path)
            language = _LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "unknown")
            current = existing.get(relative_path)

            if current is None:
                self._files.add(
                    project_id=project.id,
                    relative_path=relative_path,
                    checksum=checksum,
                    language=language,
                    size_bytes=stat.st_size,
                    modified_ns=stat.st_mtime_ns,
                )
                added += 1
                continue

            if current.checksum == checksum:
                current.size_bytes = stat.st_size
                current.modified_ns = stat.st_mtime_ns
                current.language = language
                unchanged += 1
                continue

            current.checksum = checksum
            current.language = language
            current.size_bytes = stat.st_size
            current.modified_ns = stat.st_mtime_ns
            updated += 1

        removed = self._files.remove_not_in(project.id, seen)
        project.last_scanned_at = datetime.now(UTC)
        self._session.flush()

        return ScanResult(
            added=added,
            updated=updated,
            unchanged=unchanged,
            removed=removed,
        )

    @staticmethod
    def _normalize_directory(path: Path) -> Path:
        normalized = path.expanduser().resolve()
        if not normalized.exists():
            raise SourceDirectoryError(f"Source directory does not exist: {normalized}")
        if not normalized.is_dir():
            raise SourceDirectoryError(f"Source path is not a directory: {normalized}")
        return normalized

    @staticmethod
    def _iter_source_files(root: Path) -> list[Path]:
        files: list[Path] = []
        for path in root.rglob("*"):
            relative_parts = path.relative_to(root).parts
            if any(part in _IGNORED_DIRECTORY_NAMES for part in relative_parts[:-1]):
                continue
            if path.is_file() and not path.name.startswith("."):
                files.append(path)
        return sorted(files, key=lambda item: item.relative_to(root).as_posix().casefold())

    @staticmethod
    def _checksum(path: Path) -> str:
        digest = sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
