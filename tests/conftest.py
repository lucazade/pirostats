import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


import pytest


@pytest.fixture(autouse=True)
def _no_desktop_geom(monkeypatch, tmp_path):
    """The desktop widget's geometry file lives in the real runtime dir: a developer
    running a desktop widget must not change what the tests compute."""
    import config
    monkeypatch.setattr(config, "DESKTOP_GEOM_FILE", tmp_path / "no-desktop-geom")
