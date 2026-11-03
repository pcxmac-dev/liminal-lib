"""Every module of the library imports on its own, with no program of Liminal's installed (the app,
the integrator, a plugin): the library depends on none of them. The suite proper still runs in
the app's repository with the library installed, and moves here module by module."""
from __future__ import annotations

import importlib
import pkgutil
import sys

import liminal_lib


def test_every_module_imports_without_the_app():
    names = [m.name for m in pkgutil.walk_packages(liminal_lib.__path__, "liminal_lib.")]
    assert len(names) > 40
    for name in names:
        importlib.import_module(name)
    # the library reaches into no program of Liminal's: not the app (liminal_app, once liminal), not
    # the integrator; plugins import the library as liminal_lib (2026-11-02)
    for program in ("liminal_app", "liminal_integrator", "liminal.gui", "liminal.cli"):
        assert program not in sys.modules


def test_the_version_and_the_thread_setting():
    import os

    assert liminal_lib.__version__ == "1.2.0"
    assert os.environ.get("OPENBLAS_NUM_THREADS") == "1"  # set before numpy loads, by the library
