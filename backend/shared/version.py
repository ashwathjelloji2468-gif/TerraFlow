"""Single source of code/contract version provenance (Feature 12).

`code_version()` is the short git commit of the repository this code is running from (resolved
against the repo root, not the process's working directory), or `"unknown"` when git or the
repository is unavailable (e.g. an exported tarball) -- never a made-up version. `code_dirty()`
reports uncommitted changes to tracked files, so a result produced from modified code can be told
apart from one produced from a clean commit."""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_VERSION = "0.3.0"  # docs/handoff_contract.md; same value every module already stamps
UNKNOWN = "unknown"


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, cwd=REPO_ROOT, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


@lru_cache(maxsize=1)
def code_version() -> str:
    return _git("rev-parse", "--short", "HEAD") or UNKNOWN


@lru_cache(maxsize=1)
def code_dirty() -> bool | None:
    """True if tracked files differ from HEAD; None when git is unavailable."""
    status = _git("status", "--porcelain", "--untracked-files=no")
    return None if status is None else bool(status)


def version_info() -> dict:
    """`{code_version, code_dirty, contract_version}` for provenance blocks."""
    return {"code_version": code_version(), "code_dirty": code_dirty(), "contract_version": CONTRACT_VERSION}
