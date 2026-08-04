"""Top-level ask command smoke test."""

from pathlib import Path

from typer.testing import CliRunner

from open1c_analyzer.cli.main import app

runner = CliRunner()


def test_ask_dry_run_command(database_path: Path, tmp_path: Path) -> None:
    source = tmp_path / "configuration"
    metadata = source / "DataProcessors" / "ПечатьСчетНаОплату.xml"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        "<MetaDataObject><DataProcessor><Properties>"
        "<Name>ПечатьСчетНаОплату</Name><Synonym>Печать счета на оплату</Synonym>"
        "</Properties></DataProcessor></MetaDataObject>",
        encoding="utf-8",
    )
    module = source / "DataProcessors" / "ПечатьСчетНаОплату" / "Ext" / "ManagerModule.bsl"
    module.parent.mkdir(parents=True)
    module.write_text(
        'Процедура СформироватьПФ()\n    Сообщить("Печать");\nКонецПроцедуры',
        encoding="utf-8",
    )
    output_root = tmp_path / "runs"

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["project", "add", "Demo", str(source)]).exit_code == 0
    assert runner.invoke(app, ["project", "analyze", "Demo"]).exit_code == 0

    asked = runner.invoke(
        app,
        [
            "ask",
            "Demo",
            "Где изменяется печать счета на оплату?",
            "--dry-run",
            "--output-root",
            str(output_root),
            "--max-chars",
            "20000",
            "--max-prompt-chars",
            "50000",
        ],
    )

    assert asked.exit_code == 0, asked.output
    assert "Selected entry point" in asked.stdout
    assert "Dry run" in asked.stdout
    assert list(output_root.glob("*/context.json"))
