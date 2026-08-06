from pathlib import Path
from unittest.mock import patch

import pytest

from core.preview_thumbnail import get_history_thumbnail, history_thumbnail_path


def test_thumbnail_cache_is_outside_preview_directory_and_reused(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    preview = run_root / "Previews" / "Processed" / "job_preview.png"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"source")
    expected = history_thumbnail_path(preview, run_root=run_root)

    def create_thumbnail(command: list[str], **_kwargs: object) -> None:
        Path(command[-1]).write_bytes(b"webp")

    with patch("core.preview_thumbnail.shutil.which", return_value="/usr/bin/magick"), patch(
        "core.preview_thumbnail.run_command", side_effect=create_thumbnail
    ) as run:
        assert get_history_thumbnail(preview, run_root=run_root) == expected
        assert get_history_thumbnail(preview, run_root=run_root) == expected

    assert expected.read_bytes() == b"webp"
    assert expected.parent == run_root / "output_report" / "history_thumbnails"
    assert run.call_count == 1
    assert "480x480>" in run.call_args.args[0]


def test_thumbnail_cache_rejects_preview_outside_run(tmp_path: Path) -> None:
    preview = tmp_path / "outside.png"
    preview.write_bytes(b"source")

    with pytest.raises(ValueError, match="за пределами"):
        history_thumbnail_path(preview, run_root=tmp_path / "run")
