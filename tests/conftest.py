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
    """Session-wide QApplication for tests that need Qt internals.

    A full QApplication (not just QGuiApplication) so tests that need
    actual widgets (NoteWindow, etc.) can share the same instance too
    — PySide6 only allows ONE QCoreApplication-derived singleton per
    process, so a second, separately-created QApplication instance
    later (even via `QApplication.instance() or QApplication(...)`,
    which happily returns the mismatched existing instance) crashes
    the whole test run rather than failing a single test. QApplication
    is a strict superset of QGuiApplication, so this doesn't change
    anything for tests that only needed the Gui-level features.

    Skips the test if PySide6 isn't importable (CI lint runner without
    full deps).
    """
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        pytest.skip("PySide6 not installed")
    app = QApplication.instance()
    if app is None:
        # QApplication needs argv (any list will do).
        app = QApplication([])
    yield app
    # Don't quit — pytest may reuse it across tests in the same session.
