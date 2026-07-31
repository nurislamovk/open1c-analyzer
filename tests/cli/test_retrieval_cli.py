"""Top-level retrieval command smoke tests."""

from pathlib import Path

from typer.testing import CliRunner

from open1c_analyzer.cli.main import app

runner = CliRunner()


def test_find_audit_and_context_commands(database_path: Path, tmp_path: Path) -> None:
    source = tmp_path / "configuration"
    metadata = source / "CommonModules" / "API.xml"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        "<MetaDataObject><CommonModule><Properties><Name>API</Name></Properties></CommonModule></MetaDataObject>",
        encoding="utf-8",
    )
    module = source / "CommonModules" / "API" / "Ext" / "Module.bsl"
    module.parent.mkdir(parents=True)
    module.write_text(
        'Процедура Проверить() Экспорт\n    Сообщить("OK");\nКонецПроцедуры',
        encoding="utf-8",
    )

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["project", "add", "Demo", str(source)]).exit_code == 0
    assert runner.invoke(app, ["project", "analyze", "Demo"]).exit_code == 0

    found = runner.invoke(app, ["find", "Demo", "Проверить"])
    assert found.exit_code == 0, found.output
    assert "Проверить" in found.stdout

    audited = runner.invoke(app, ["audit", "Demo"])
    assert audited.exit_code == 0, audited.output
    assert "built in" in audited.stdout

    output = tmp_path / "context.json"
    context = runner.invoke(
        app,
        ["context", "Demo", "Проверить", "--output", str(output)],
    )
    assert context.exit_code == 0, context.output
    assert output.exists()
