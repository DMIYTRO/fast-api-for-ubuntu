import os
import sys
import shutil
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control_panel import PREVIEW_CACHE_DIR, get_cached_preview_path

def test_get_cached_preview_path_creates_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr("control_panel.PREVIEW_CACHE_DIR", cache_dir)
    
    source = tmp_path / "source.png"
    source.write_bytes(b"preview-data")
    
    cached = get_cached_preview_path(source)
    
    assert cached != source
    assert cached.parent == cache_dir
    assert cached.is_file()
    assert cached.read_bytes() == b"preview-data"
    
    cached_again = get_cached_preview_path(source)
    assert cached_again == cached

def test_get_cached_preview_path_updates_on_modification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr("control_panel.PREVIEW_CACHE_DIR", cache_dir)
    
    source = tmp_path / "source.png"
    source.write_bytes(b"v1")
    cached1 = get_cached_preview_path(source)
    assert cached1.read_bytes() == b"v1"
    
    source.write_bytes(b"v2_new_content")
    cached2 = get_cached_preview_path(source)
    assert cached2.read_bytes() == b"v2_new_content"

def test_get_cached_preview_path_fallback_nonexistent(tmp_path: Path):
    non_existent = tmp_path / "does_not_exist.png"
    result = get_cached_preview_path(non_existent)
    assert result == non_existent
