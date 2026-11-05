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


def _identify(path: Path, fmt: str) -> list[str]:
    import shutil
    import subprocess

    magick = shutil.which("magick")
    assert magick
    return subprocess.run(
        [magick, "identify", "-format", fmt, str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.split("|")


def _share_env(share: Path):
    return patch.dict("os.environ", {"IMAGE_MAGIC_INPUT_DIR": str(share)}), patch(
        "core.preview_cache._is_external_mount", return_value=True
    )


def test_jpeg_output_arguments_are_explicit_in_command(tmp_path: Path) -> None:
    thumbnail = tmp_path / "Previews" / "preview.jpg"
    with patch("core.preview_generator.shutil.which", return_value="/usr/bin/magick"), patch(
        "core.preview_generator.run_command"
    ) as run_command, patch("core.preview_generator.preview_work_dir", return_value=tmp_path):
        generate_preview(
            "source.png", str(thumbnail), dpi=300, w_px=2400, h_px=1600,
            full_preview_path=str(tmp_path / "full.jpg"), preview_format="jpeg",
        )
    for call, limit in zip(run_command.call_args_list, (PREVIEW_MAX_PIXELS, PREVIEW_FULL_MAX_PIXELS)):
        cmd = call.args[0]
        assert cmd[cmd.index("-resize") + 1] == f"{limit}x{limit}>"
        assert cmd[cmd.index("-background") + 1] == "white"
        assert "remove" in cmd and "off" in cmd
        assert cmd[cmd.index("-sampling-factor") + 1] == "1x1"
        assert cmd[cmd.index("-quality") + 1] == "95"
        assert cmd[-1].endswith(".jpg")


def test_png_output_has_no_jpeg_options_by_default(tmp_path: Path) -> None:
    with patch("core.preview_generator.shutil.which", return_value="/usr/bin/magick"), patch(
        "core.preview_generator.run_command"
    ) as run_command, patch("core.preview_generator.preview_work_dir", return_value=tmp_path):
        generate_preview("source.png", str(tmp_path / "p.png"), dpi=300, w_px=2400, h_px=1600)
    cmd = run_command.call_args.args[0]
    assert "-quality" not in cmd and "-sampling-factor" not in cmd
    assert cmd[-1].endswith("p.png")


def test_format_must_match_suffix_and_thumbnail_full_must_match(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError):
        generate_preview("s.png", str(tmp_path / "p.png"), 300, 100, 100, preview_format="jpeg")
    with pytest.raises(ValueError):
        generate_preview(
            "s.png", str(tmp_path / "p.jpg"), 300, 100, 100,
            full_preview_path=str(tmp_path / "full.png"),
        )


def test_real_jpeg_render_sizes_quality_sampling_and_white_background(tmp_path: Path) -> None:
    import shutil
    import subprocess

    import pytest

    magick = shutil.which("magick")
    if not magick:
        pytest.skip("ImageMagick is required")
    share = tmp_path / "share"
    (share / "Previews").mkdir(parents=True)
    source = share / "source.png"
    # Fully transparent source: JPEG must show an explicit white backdrop.
    subprocess.run([magick, "-size", "4000x2000", "xc:none", str(source)], check=True)
    thumbnail = share / "Previews" / "source_preview.jpg"
    full = share / "Previews" / "source_full.jpg"
    env, mount = _share_env(share)
    with env, mount:
        result = generate_preview(
            str(source), str(thumbnail), dpi=300, w_px=4000, h_px=2000,
            full_preview_path=str(full), preview_format="jpeg",
        )
    assert result == str(thumbnail)
    for path, limit in ((thumbnail, 480), (full, 1800)):
        fmt, width, height, sampling = _identify(path, "%m|%w|%h|%[jpeg:sampling-factor]")
        assert fmt == "JPEG"
        assert max(int(width), int(height)) == limit
        assert sampling == "1x1,1x1,1x1"
    centre = subprocess.run(
        [magick, str(thumbnail), "-format", "%[pixel:p{240,120}]", "info:"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert centre.lower() in {"srgb(255,255,255)", "white", "#ffffff"}
    quality = _identify(thumbnail, "%Q")[0]
    assert int(quality) >= 94


def test_real_png_render_still_supported_for_historical_format(tmp_path: Path) -> None:
    import shutil
    import subprocess

    import pytest

    magick = shutil.which("magick")
    if not magick:
        pytest.skip("ImageMagick is required")
    share = tmp_path / "share"
    (share / "Previews").mkdir(parents=True)
    source = share / "source.png"
    subprocess.run([magick, "-size", "1000x500", "xc:#336699", str(source)], check=True)
    thumbnail = share / "Previews" / "source_preview.png"
    env, mount = _share_env(share)
    with env, mount:
        generate_preview(str(source), str(thumbnail), dpi=300, w_px=1000, h_px=500)
    assert _identify(thumbnail, "%m")[0] == "PNG"


def test_real_jpeg_render_of_cmyk_source_is_rgb(tmp_path: Path) -> None:
    import shutil
    import subprocess

    import pytest

    magick = shutil.which("magick")
    if not magick:
        pytest.skip("ImageMagick is required")
    share = tmp_path / "share"
    (share / "Previews").mkdir(parents=True)
    source = share / "cmyk.tif"
    subprocess.run([magick, "-size", "800x600", "xc:#336699", "-colorspace", "CMYK", str(source)], check=True)
    thumbnail = share / "Previews" / "cmyk_preview.jpg"
    env, mount = _share_env(share)
    with env, mount:
        generate_preview(str(source), str(thumbnail), dpi=300, w_px=800, h_px=600)
    assert _identify(thumbnail, "%[colorspace]")[0] == "sRGB"
