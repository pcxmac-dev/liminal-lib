"""The workspace (layout.py; the user, 2026-11-02: "common, app and store are the required folders,
plugins as well"): the databases in store/data, the plugins in plugins/, the library in common."""
from __future__ import annotations

from pathlib import Path

from liminal_lib import config, layout


def test_the_workspace_is_found_by_its_four_folders_or_named(tmp_path, monkeypatch):
    root = tmp_path / "liminal"
    for name in layout.FOLDERS:
        (root / name).mkdir(parents=True)
    monkeypatch.setenv(layout.HOME_VAR, str(root))
    monkeypatch.delenv(layout.PLUGINS_VAR, raising=False)
    assert layout.home() == root and layout.data_dir() == root / "store" / "data" and layout.plugins_dir() == root / "plugins"
    monkeypatch.delenv(layout.HOME_VAR)
    monkeypatch.setattr(layout, "FOLDERS", ("no-such-folder",))
    assert layout.home() is None and layout.data_dir() == Path.home() / "LiminalData" and layout.plugins_dir() is None
    assert config.DEFAULT_DB_PATH.name == layout.MAIN and config.DEFAULT_DB_PATH.parent.name in ("data", "LiminalData")
