"""Migration compatibility from the project-catalog release."""

from alembic import command
from sqlalchemy import create_engine, inspect

from open1c_analyzer.cli.common import alembic_config
from open1c_analyzer.config import Settings


def test_upgrade_from_project_catalog_to_analyzer_core(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(database_path=tmp_path / "open1c.db")
    config = alembic_config(settings)

    command.upgrade(config, "0002_project_source_catalog")
    command.upgrade(config, "head")

    engine = create_engine(settings.database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert {
        "call_sites",
        "dependencies",
        "metadata_objects",
        "modules",
        "queries",
        "references",
        "symbols",
    } <= tables
