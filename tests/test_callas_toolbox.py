from pathlib import Path
from unittest.mock import patch

import pytest

from core.callas_toolbox import CallasToolbox, CallasToolboxError
from server.settings import Settings


def toolbox(tmp_path: Path) -> CallasToolbox:
    settings = Settings.from_env()
    return CallasToolbox(settings.__class__(**{
        **{field: getattr(settings, field) for field in settings.__dataclass_fields__},
        "callas_cli_path": tmp_path / "pdfToolbox",
        "callas_cache_dir": tmp_path / "cache",
    }))


def test_missing_cli_has_actionable_error(tmp_path):
    with pytest.raises(CallasToolboxError, match="CLI callas не найден"):
        toolbox(tmp_path).version()


def test_merge_builds_argv_and_checks_output(tmp_path):
    cli = tmp_path / "pdfToolbox"
    cli.touch()
    first, second, output = (tmp_path / name for name in ("a.pdf", "b.pdf", "merged.pdf"))
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    def fake_run(command, **kwargs):
        output.write_bytes(b"pdf")
        return __import__("subprocess").CompletedProcess(command, 0, "ok", "")

    with patch("core.callas_toolbox.run_command", side_effect=fake_run) as run:
        result = toolbox(tmp_path).merge_pdfs([first, second], output)
    command = run.call_args.args[0]
    assert command[0] == str(cli.resolve())
    assert "--mergepdf" in command
    assert command[-2:] == [str(first.resolve()), str(second.resolve())]
    assert result.returncode == 0


def test_empty_merge_is_rejected(tmp_path):
    with pytest.raises(CallasToolboxError, match="Список PDF-файлов"):
        toolbox(tmp_path).merge_pdfs([], tmp_path / "out.pdf")
