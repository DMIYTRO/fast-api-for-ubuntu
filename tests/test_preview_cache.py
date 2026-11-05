from pathlib import Path

import pytest

from core.preview_cache import full_preview_cache_path, preview_cache_root, preview_work_dir


def test_full_rendition_cache_is_deterministic_and_stays_on_share(tmp_path, monkeypatch):
    share = tmp_path / "share"
    share.mkdir()
    cache = share / ".preview-cache"
    monkeypatch.setenv("IMAGE_MAGIC_INPUT_DIR", str(share))
    monkeypatch.setenv("IMAGE_MAGIC_PREVIEW_CACHE_DIR", str(cache))
    monkeypatch.setattr("core.preview_cache._is_external_mount", lambda _path: True)

    thumbnail = share / "Previews" / "order_preview.png"
    full = full_preview_cache_path(thumbnail)
    assert full == full_preview_cache_path(thumbnail)
    assert full.is_relative_to(cache)
    assert preview_cache_root() == cache


def test_working_files_use_external_share_and_directory_exists(tmp_path, monkeypatch):
    share = tmp_path / "share"
    share.mkdir()
    cache = share / ".preview-cache"
    monkeypatch.setenv("IMAGE_MAGIC_INPUT_DIR", str(share))
    monkeypatch.setenv("IMAGE_MAGIC_PREVIEW_CACHE_DIR", str(cache))
    monkeypatch.setattr("core.preview_cache._is_external_mount", lambda _path: True)

    work = preview_work_dir(share / "Previews" / "order.png")
    assert work.is_relative_to(cache)
    assert work.is_dir()


def test_local_cache_configuration_falls_back_to_external_share(tmp_path, monkeypatch, caplog):
    share = tmp_path / "share"
    share.mkdir()
    monkeypatch.setenv("IMAGE_MAGIC_INPUT_DIR", str(share))
    local_cache = tmp_path / "system-cache"
    monkeypatch.setenv("IMAGE_MAGIC_PREVIEW_CACHE_DIR", str(local_cache))
    monkeypatch.setattr("core.preview_cache._is_external_mount", lambda _path: True)

    cache = preview_cache_root()
    assert cache == share / ".preview-cache"
    assert not local_cache.exists()
    assert "preview.cache_path_outside_share" in caplog.text


def test_unmounted_external_share_is_a_hard_error(tmp_path, monkeypatch):
    share = tmp_path / "share"
    share.mkdir()
    monkeypatch.setenv("IMAGE_MAGIC_INPUT_DIR", str(share))
    monkeypatch.setattr("core.preview_cache._is_external_mount", lambda _path: False)

    with pytest.raises(RuntimeError, match="Внешний диск Share недоступен"):
        preview_cache_root()


def test_full_rendition_cache_extension_follows_thumbnail_format(tmp_path, monkeypatch):
    share = tmp_path / "share"
    share.mkdir()
    monkeypatch.setenv("IMAGE_MAGIC_INPUT_DIR", str(share))
    monkeypatch.setattr("core.preview_cache._is_external_mount", lambda _path: True)

    png = full_preview_cache_path(share / "Previews" / "order_preview.png")
    jpg = full_preview_cache_path(share / "Previews" / "order_preview.jpg")
    jpeg = full_preview_cache_path(share / "Previews" / "order_preview.jpeg")
    assert png.suffix == ".png"  # historical PNG caches keep resolving
    assert jpg.suffix == ".jpg" and jpeg.suffix == ".jpg"
    assert png.stem != jpg.stem  # different thumbnail path => different key


def test_preview_format_setting_defaults_to_png_and_is_configurable(monkeypatch):
    from core.preview_cache import (
        default_preview_format,
        normalize_preview_format,
        preview_extension,
        preview_jpeg_quality,
        preview_media_type,
        with_preview_extension,
    )

    monkeypatch.delenv("IMAGE_MAGIC_PREVIEW_FORMAT", raising=False)
    monkeypatch.delenv("IMAGE_MAGIC_PREVIEW_JPEG_QUALITY", raising=False)
    assert default_preview_format() == "png"
    assert preview_extension() == ".png"
    assert preview_jpeg_quality() == 95

    monkeypatch.setenv("IMAGE_MAGIC_PREVIEW_FORMAT", "JPG")
    monkeypatch.setenv("IMAGE_MAGIC_PREVIEW_JPEG_QUALITY", "90")
    assert default_preview_format() == "jpeg"
    assert preview_extension() == ".jpg"
    assert preview_extension("png") == ".png"
    assert preview_jpeg_quality() == 90
    assert with_preview_extension("a/x_preview.png") == Path("a/x_preview.jpg")

    monkeypatch.setenv("IMAGE_MAGIC_PREVIEW_FORMAT", "gif")  # invalid -> safe default
    assert default_preview_format() == "png"
    with pytest.raises(ValueError):
        normalize_preview_format("gif")

    assert preview_media_type("a.jpg") == "image/jpeg"
    assert preview_media_type("a.JPEG") == "image/jpeg"
    assert preview_media_type("a.png") == "image/png"


def test_preview_variants_lists_png_and_jpeg_twins_newest_first(tmp_path):
    import os

    from core.preview_cache import preview_name_key, preview_variants

    old = tmp_path / "x_preview.png"
    new = tmp_path / "x_preview.jpg"
    old.write_bytes(b"png")
    new.write_bytes(b"jpg")
    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    os.utime(new, ns=(2_000_000_000, 2_000_000_000))
    assert preview_variants(tmp_path / "x_preview") == [new, old]
    assert preview_variants(tmp_path / "missing_preview") == []
    assert preview_name_key("x_preview.JPG") == preview_name_key("x_preview.png") == "x_preview"
