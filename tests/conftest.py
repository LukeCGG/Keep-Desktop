"""Shared pytest fixtures + path setup.

The tests run from the repo root so that ``import keep_protocol`` /
``import keep_sync_v2`` work the same way they do at runtime.
"""

from __future__ import annotations

import os
import sys

# Add the repo root to sys.path so the bare-package imports resolve
# whether pytest is invoked from the repo root or `tests/`.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# A few tests need a Qt application instance (html_to_styled_doc spins
# up a QTextDocument which needs QGuiApplication to be alive). Provide
# one session-scoped fixture so each test doesn't spin its own.
import pytest


@pytest.fixture(scope="session")
def qapp():
    """Session-wide QGuiApplication for tests that need Qt internals.

    Skips the test if PySide6 isn't importable (CI lint runner without
    full deps).
    """
    try:
        from PySide6.QtGui import QGuiApplication
    except ImportError:
        pytest.skip("PySide6 not installed")
    app = QGuiApplication.instance()
    if app is None:
        # QGuiApplication needs argv (any list will do).
        app = QGuiApplication([])
    yield app
    # Don't quit — pytest may reuse it across tests in the same session.
