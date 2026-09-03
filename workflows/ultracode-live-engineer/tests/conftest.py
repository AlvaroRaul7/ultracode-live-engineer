import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import cache as cache_module  # noqa: E402


@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    """Point lib.cache.CACHE_PATH at an isolated tmp file for this test."""
    path = str(tmp_path / "ultracode-cache.json")
    monkeypatch.setattr(cache_module, "CACHE_PATH", path)
    return path
