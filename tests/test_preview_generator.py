from pathlib import Path
from unittest.mock import patch

from core.preview_generator import (
    PREVIEW_FULL_MAX_PIXELS,
    PREVIEW_MAX_PIXELS,
    FoldOverlay,
    _fold_draw_commands,
    generate_preview,
)
from processing.batch_processor import BatchProcessor
from processing.models import FileCheck, ParsedFilename


def test_confirmed_fold_replaces_both_standard_preview_frames(tmp_path: Path) -> None:
    with patch("core.preview_generator.shutil.which", return_value="/usr/bin/magick"), patch(
        "core.preview_generator.run_command"
    ) as run_command:
        generate_preview(
            "source.png",
            str(tmp_path / "preview.png"),
            dpi=300,
            w_px=900,
            h_px=600,
            fold_overlay={
                "type": "c-fold",
                "count": 2,
                "confirmed": True,
                "span_mm": 300,
            },
        )

    command = run_command.call_args.args[0]
    assert "#ff00ff" in command
    assert "#00ff00" in command
    assert "#28a745" not in command
    assert "#dc3545" not in command
    assert "line 294.00,0 294.00,600" in command
    assert "line 594.00,0 594.00,600" in command
    assert "rectangle 47.00,47 247.00,553" in command
    assert ["-resize", f"{PREVIEW_MAX_PIXELS}x{PREVIEW_MAX_PIXELS}>"] == command[-3:-1]


def test_unconfirmed_or_unsupported_fold_keeps_existing_frames(tmp_path: Path) -> None:
    with patch("core.preview_generator.shutil.which", return_value="/usr/bin/magick"), patch(
        "core.preview_generator.run_command"
    ) as run_command:
        generate_preview(
            "source.png",
            str(tmp_path / "preview.png"),
            dpi=300,
            w_px=900,
            h_px=600,
            fold_overlay={"type": "window-fold", "count": 2, "confirmed": True},
        )

    command = run_command.call_args.args[0]
    assert "#28a745" in command
    assert "#dc3545" in command
    assert "rectangle" in " ".join(command)


def test_c_fold_back_reverses_panel_geometry_and_height_axis_is_supported() -> None:
    face = FoldOverlay("c-fold", 2, True, "face", "width", 300)
    back = FoldOverlay("c-fold", 2, True, "back", "height", 300)

    assert _fold_draw_commands(900, 600, face) == [
        "line 294.00,0 294.00,600",
        "line 594.00,0 594.00,600",
    ]
    assert _fold_draw_commands(900, 600, back) == [
        "line 0,204.00 900,204.00",
        "line 0,404.00 900,404.00",
    ]


def test_source_preview_overlay_uses_parsed_side_and_finished_span(tmp_path: Path) -> None:
    file_check = FileCheck(
        path=tmp_path / "sample-back.jpg",
        parsed=ParsedFilename("100", "200", 297, 210, 4, 4, "back"),
    )

    overlay = BatchProcessor._overlay_for_file(
        {"type": "c-fold", "count": 2, "confirmed": True}, file_check
    )

    assert overlay == {
        "type": "c-fold",
        "count": 2,
        "confirmed": True,
        "side": "back",
        "span_mm": 297,
    }


def test_full_rendition_uses_independent_size_and_share_scratch(tmp_path: Path) -> None:
    share = tmp_path / "share"
    share.mkdir()
    thumbnail = share / "Previews" / "preview.png"
    cache = share / ".cache"
    with patch.dict("os.environ", {
        "IMAGE_MAGIC_INPUT_DIR": str(share),
        "IMAGE_MAGIC_PREVIEW_CACHE_DIR": str(cache),
    }), patch("core.preview_cache._is_external_mount", return_value=True), patch("core.preview_generator.shutil.which", return_value="/usr/bin/magick"), patch(
        "core.preview_generator.run_command"
    ) as run_command:
        from core.preview_cache import full_preview_cache_path
        full_path = full_preview_cache_path(thumbnail)
        generate_preview(
            "source.png", str(thumbnail), dpi=300, w_px=2400, h_px=1600,
            full_preview_path=str(full_path),
        )

    assert run_command.call_count == 2
    compact_cmd = run_command.call_args_list[0].args[0]
    full_cmd = run_command.call_args_list[1].args[0]
    assert compact_cmd[-3:-1] == ["-resize", f"{PREVIEW_MAX_PIXELS}x{PREVIEW_MAX_PIXELS}>"]
    assert full_cmd[-3:-1] == ["-resize", f"{PREVIEW_FULL_MAX_PIXELS}x{PREVIEW_FULL_MAX_PIXELS}>"]
    scratch_path = Path(run_command.call_args_list[0].kwargs["env"]["MAGICK_TEMPORARY_PATH"])
    assert scratch_path.is_relative_to(cache / ".work")
    assert not scratch_path.exists()
    assert full_path.is_relative_to(cache)


def test_preview_cache_rejects_configured_local_disk_path(tmp_path: Path) -> None:
    from core.preview_cache import preview_cache_root
    share = tmp_path / "share"
    share.mkdir()
    with patch.dict("os.environ", {
        "IMAGE_MAGIC_INPUT_DIR": str(share),
        "IMAGE_MAGIC_PREVIEW_CACHE_DIR": str(tmp_path / "system-cache"),
    }), patch("core.preview_cache._is_external_mount", return_value=True):
        cache = preview_cache_root()
        assert cache == share / ".preview-cache"
