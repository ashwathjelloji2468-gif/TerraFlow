"""Shared fixtures for M0 job-system tests.

Every test gets its own `data/` directory (via `SIH26_DATA_DIR`), so the
registry and run folders never touch the real `data/`. Fake stage and fake
solver timings are shrunk so a whole onboarding job runs in about a second.
"""

from __future__ import annotations

import time
from typing import Callable

import pytest

from backend.m0_api import registry


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SIH26_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SIH26_FAKE_STAGE_S", "0.01")
    monkeypatch.setenv("SIH26_FAKE_N_RUNS", "3")
    monkeypatch.setenv("SIH26_FAKE_RUN_STEPS", "3")
    monkeypatch.setenv("SIH26_FAKE_RUN_STEP_S", "0.05")
    # Feature 2: no test may reach a real data service; every product that would need the network
    # is recorded `unavailable` with this reason instead (backend/m1_terrain/ingest.py).
    monkeypatch.setenv("SIH26_INGEST_NETWORK", "off")
    registry.init_db()
    return tmp_path / "data"


@pytest.fixture
def conn(data_dir):
    c = registry.connect()
    yield c
    c.close()


def wait_until(condition: Callable[[], bool], step: Callable[[], None] | None = None, timeout_s: float = 15.0) -> None:
    """Call `step` (e.g. `worker.tick`) until `condition()` holds, or fail."""
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached before timeout")
        if step is not None:
            step()
        time.sleep(0.02)
