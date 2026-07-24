"""CLI tests."""

from pathlib import Path

from typer.testing import CliRunner

from open1c_analyzer.cli.main import app
from open1c_analyzer.version import __version__

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_init_creates_database(database_path: Path) -> None:
    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert database_path.exists()
    assert "initialized" in result.stdout


def test_doctor_after_init(database_path: Path) -> None:
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.output

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "SQLite connection" in result.stdout
    assert database_path.exists()
