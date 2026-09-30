"""Mock response data for M0: contracts/examples/*.json, patched with the IDs
from the actual request so responses look consistent with what the client
asked for.

Used by the endpoints that are still mocked. Not used for sites or jobs: `POST /sites`,
`GET /sites[/{id}]` and `GET /jobs/{id}` are real (`site_registry.py`, `jobs.py`, `worker.py`). There are no real M1-M7 outputs behind these mocks
yet (CLAUDE.md rule 2: "every module runs end-to-end on synthetic data ...
before real data exists").
"""

from __future__ import annotations

import copy
from typing import Any

from backend.m0_api import schemas

# Which sites exist is no longer decided here: `backend/m0_api/site_registry.py` serves every
# bundled (`config/sites.yaml`) or onboarded (`POST /sites`) site.

_ID_KEYS = {"site_id", "query_id", "job_id", "event_id", "run_id", "scenario_id"}


def _patch_ids(node: Any, ids: dict[str, str]) -> Any:
    if isinstance(node, dict):
        return {
            k: (ids[k] if k in _ID_KEYS and k in ids and isinstance(v, str) else _patch_ids(v, ids))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_patch_ids(v, ids) for v in node]
    return node


def mock_response(example_name: str, **ids: str) -> Any:
    """Deep-copy an example payload and patch its ID fields to match the
    request path/body, so e.g. GET /sites/rishiganga doesn't come back
    labelled "teesta"."""
    data = copy.deepcopy(schemas.load_example(example_name))
    return _patch_ids(data, ids) if ids else data


def error(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}
