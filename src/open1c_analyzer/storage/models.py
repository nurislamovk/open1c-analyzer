"""SQLAlchemy models for the local 1C knowledge graph."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative model base."""


class Project(Base):
    """A registered 1C source export."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, default=lambda: str(uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    source_path: Mapped[str | None] = mapped_column(String(2048), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_analyzed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    profile_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    files: Mapped[list["SourceFile"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )


class SourceFile(Base):
    """A file discovered inside a registered source export."""

    __tablename__ = "source_files"
    __table_args__ = (
        UniqueConstraint("project_id", "relative_path", name="uq_source_files_project_path"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relative_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    modified_ns: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    analyzed_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    analysis_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    project: Mapped[Project] = relationship(back_populates="files")


class MetadataObject(Base):
    """A configuration metadata object or child object."""

    __tablename__ = "metadata_objects"
    __table_args__ = (
        UniqueConstraint("project_id", "full_name", name="uq_metadata_project_full_name"),
        Index("ix_metadata_project_kind_name", "project_id", "kind", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_file_id: Mapped[int] = mapped_column(
        ForeignKey("source_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    uuid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(2048), nullable=False, index=True)
    parent_full_name: Mapped[str | None] = mapped_column(String(2048), nullable=True, index=True)
    synonym: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    properties_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class Module(Base):
    """A BSL module associated with a configuration object."""

    __tablename__ = "modules"
    __table_args__ = (
        UniqueConstraint("source_file_id", name="uq_modules_source_file"),
        Index("ix_modules_project_owner", "project_id", "owner_kind", "owner_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_file_id: Mapped[int] = mapped_column(
        ForeignKey("source_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metadata_object_id: Mapped[int | None] = mapped_column(
        ForeignKey("metadata_objects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(2048), nullable=False, index=True)
    module_kind: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    owner_kind: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    owner_name: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    directives_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")


class Symbol(Base):
    """A procedure or function declared in BSL."""

    __tablename__ = "symbols"
    __table_args__ = (
        Index("ix_symbols_project_name", "project_id", "normalized_name"),
        Index("ix_symbols_module_name", "module_id", "normalized_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    module_id: Mapped[int] = mapped_column(
        ForeignKey("modules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_file_id: Mapped[int] = mapped_column(
        ForeignKey("source_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    normalized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    is_export: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    directive: Mapped[str | None] = mapped_column(String(128), nullable=True)
    region: Mapped[str | None] = mapped_column(String(512), nullable=True)
    line_start: Mapped[int] = mapped_column(Integer, nullable=False)
    line_end: Mapped[int] = mapped_column(Integer, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    parameters_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class CallSite(Base):
    """A method call found in BSL."""

    __tablename__ = "call_sites"
    __table_args__ = (Index("ix_calls_project_target", "project_id", "normalized_name"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_file_id: Mapped[int] = mapped_column(
        ForeignKey("source_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    caller_symbol_id: Mapped[int | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), nullable=True, index=True
    )
    resolved_symbol_id: Mapped[int | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="SET NULL"), nullable=True, index=True
    )
    callee_name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    qualifier: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    full_name: Mapped[str] = mapped_column(String(1536), nullable=False)
    line: Mapped[int] = mapped_column(Integer, nullable=False)
    column: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution: Mapped[str] = mapped_column(String(64), nullable=False, default="unresolved")


class QueryFact(Base):
    """A query-language text embedded in BSL."""

    __tablename__ = "queries"
    __table_args__ = (Index("ix_queries_project_kind", "project_id", "kind"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_file_id: Mapped[int] = mapped_column(
        ForeignKey("source_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol_id: Mapped[int | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), nullable=True, index=True
    )
    line_start: Mapped[int] = mapped_column(Integer, nullable=False)
    line_end: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tables_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")


class Reference(Base):
    """A BSL or metadata reference to another configuration object."""

    __tablename__ = "references"
    __table_args__ = (Index("ix_references_project_target", "project_id", "normalized_target"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_file_id: Mapped[int] = mapped_column(
        ForeignKey("source_files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol_id: Mapped[int | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), nullable=True, index=True
    )
    source_metadata_name: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    metadata_object_id: Mapped[int | None] = mapped_column(
        ForeignKey("metadata_objects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_kind: Mapped[str] = mapped_column(String(96), nullable=False)
    target_name: Mapped[str] = mapped_column(String(512), nullable=False)
    target_full_name: Mapped[str] = mapped_column(String(1024), nullable=False)
    normalized_target: Mapped[str] = mapped_column(String(1024), nullable=False)
    relation: Mapped[str] = mapped_column(String(64), nullable=False)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_value: Mapped[str | None] = mapped_column(String(2048), nullable=True)


class Dependency(Base):
    """A resolved or unresolved edge in the project knowledge graph."""

    __tablename__ = "dependencies"
    __table_args__ = (
        Index("ix_dependencies_project_source", "project_id", "source_name"),
        Index("ix_dependencies_project_target", "project_id", "target_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_files.id", ondelete="CASCADE"), nullable=True, index=True
    )
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_name: Mapped[str] = mapped_column(String(2048), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_name: Mapped[str] = mapped_column(String(2048), nullable=False)
    relation: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
