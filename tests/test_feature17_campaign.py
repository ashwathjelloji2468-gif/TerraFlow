"""Feature 17 (audit A7, decision E5): rerunning a campaign never destroys a failed M3 attempt."""
from __future__ import annotations

import json

from backend import campaign
from backend.m0_api import registry
from tests.test_campaign_isolation import synth_campaign  # noqa: F401  (fixture)


def test_solver_failed_run_is_archived_before_rebuild(synth_campaign):  # noqa: F811
    data_dir, sites_dir = synth_campaign
    conn = registry.connect()
    campaign.run_dflowfm_campaign("synth", conn, data_dir, sites_dir)
    run_dir = data_dir / "synth/runs/synth__s001__delft3d"
    (run_dir / "case/output").mkdir(parents=True)
    (run_dir / "case/output/model.dia").write_text("** ERROR : real solver failure evidence\n")
    (run_dir / "log.txt").write_text("attempt log\n")
    with conn:  # a solver failure (not FAILED_ACCEPTANCE): not preserved, so it is rebuilt ...
        conn.execute("UPDATE runs SET status='failed', meta_json='{}' WHERE run_id='synth__s001__delft3d'")
    _, again = campaign.run_dflowfm_campaign("synth", conn, data_dir, sites_dir)
    assert {r.scenario_id: r.status for r in again}["synth__s001"] == "queued"
    archived = run_dir / "archive" / "rebuild_01"  # ... but only after its evidence was archived intact
    assert (archived / "case/output/model.dia").read_text().startswith("** ERROR")
    assert (archived / "log.txt").read_text() == "attempt log\n"
    assert json.loads((archived / "archive_meta.json").read_text())["previous_registry_status"] == "failed"
    prov = json.loads((run_dir / "scenario.json").read_text())["provenance"]
    assert prov["archived_previous_run"].endswith("rebuild_01")
    conn.close()
