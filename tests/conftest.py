"""Collect destroyed Tk interpreter cycles on the main test thread.

Tests create independent Tk roots. Without explicit collection between tests,
Python can collect a prior root's Variables in a later background worker, which
Tk rejects (and can abort the entire process on Windows).
"""
import gc

import pytest


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_teardown(item, nextitem):
    yield
    gc.collect()
