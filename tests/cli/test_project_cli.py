"""Project CLI tests."""

from pathlib import Path

from typer.testing import CliRunner

from open1c_analyzer.cli.main import app

runner = CliRunner()


def test_project_add_scan_show_list_and_remove(database_path: Path, tmp_path: Path) -> None:
    source_path = tmp_path / "configuration"
    source_path.mkdir()
    (source_path / "Configuration.xml").write_text("<Configuration />", encoding="utf-8")
    (source_path / "Module.bsl").write_text("procedure Test()", encoding="utf-8")

    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.output

    add_result = runner.invoke(app, ["project", "add", "Demo", str(source_path)])
    assert add_result.exit_code == 0, add_result.output
    assert "Project added" in add_result.stdout

    scan_result = runner.invoke(app, ["project", "scan", "Demo"])
    assert scan_result.exit_code == 0, scan_result.output
    assert "added: 2" in scan_result.stdout

    show_result = runner.invoke(app, ["project", "show", "Demo"])
    assert show_result.exit_code == 0, show_result.output
    assert "Files: 2" in show_result.stdout

    list_result = runner.invoke(app, ["project", "list"])
    assert list_result.exit_code == 0, list_result.output
    assert "Demo" in list_result.stdout

    remove_result = runner.invoke(app, ["project", "remove", "Demo", "--yes"])
    assert remove_result.exit_code == 0, remove_result.output
    assert "Project removed" in remove_result.stdout


def test_project_add_reports_invalid_directory(database_path: Path, tmp_path: Path) -> None:
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0, init_result.output

    result = runner.invoke(app, ["project", "add", "Missing", str(tmp_path / "missing")])

    assert result.exit_code == 1
    assert "does not exist" in result.stdout
