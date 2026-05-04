"""Smoke tests: every module must at least import cleanly.

Catches the kind of silent breakage that bit us in v2.0.0: bogus
requirements lines, circular imports, top-level NameErrors, etc.
"""

from __future__ import annotations

import importlib

import pytest


# Modules that must always import.
_PURE_PY_MODULES = [
    "config",
    "keep_sync",
    "keep_sync_v2",
    "updater",
    "keep_protocol",
    "keep_protocol.auth",
    "keep_protocol.client",
    "keep_protocol.models",
    "keep_protocol.nested_model",
]

# Modules that need Qt — skip if PySide6 isn't installed.
_QT_MODULES = [
    "main",
    "app_controller",
    "app_icon",
    "note_window",
]


@pytest.mark.parametrize("modname", _PURE_PY_MODULES)
def test_pure_module_imports(modname):
    importlib.import_module(modname)


@pytest.mark.parametrize("modname", _QT_MODULES)
def test_qt_module_imports(modname, qapp):  # noqa: ARG001 — qapp fixture init
    importlib.import_module(modname)


def test_app_version_is_string():
    from config import APP_VERSION
    assert isinstance(APP_VERSION, str)
    assert APP_VERSION.count(".") >= 2  # X.Y.Z
    parts = APP_VERSION.split(".")
    for p in parts[:3]:
        assert p.isdigit(), f"non-numeric version component: {p!r}"
