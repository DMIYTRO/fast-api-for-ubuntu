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
