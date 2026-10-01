"""Every contracts/examples/*.json file must validate against its matching
contracts/schemas/*.json file (contract §8: "a valid example file for
each")."""

from __future__ import annotations

import pytest

from backend.m0_api import schemas

# example file -> schema file. Kept explicit (not glob-matched by stripping
# suffixes) so a rename on one side without the other fails loudly here.
EXAMPLE_TO_SCHEMA = {
    "health.example.json": "health.schema.json",
    "error.example.json": "error.schema.json",
    "site_summary.example.json": "site_summary.schema.json",
    "site_list.example.json": "site_list.schema.json",
    "site_detail.example.json": "site_detail.schema.json",
    "site_create_request.example.json": "site_create_request.schema.json",
    "site_create_accepted.example.json": "site_create_accepted.schema.json",
    "job_accepted.example.json": "job_accepted.schema.json",
    "recheck_request.example.json": "recheck_request.schema.json",
    "job_status.example.json": "job_status.schema.json",
    "flood_query_request.example.json": "flood_query_request.schema.json",
    "flood_query_response.example.json": "flood_query_response.schema.json",
    "extent_geojson.example.json": "geojson_feature_collection.schema.json",
    "geojson_feature_collection.example.json": "geojson_feature_collection.schema.json",
    "timeline.example.json": "timeline.schema.json",
    "impact.example.json": "impact.schema.json",
    "compare.example.json": "compare.schema.json",
    "validation.example.json": "validation.schema.json",
    "historical_validation.example.json": "historical_validation.schema.json",
    "gee_layers.example.json": "gee_layers.schema.json",
    "breach_params.example.json": "breach_params.schema.json",
    "hydrograph_sidecar.example.json": "hydrograph_sidecar.schema.json",
    "scene3d.example.json": "scene3d.schema.json",
    "run_meta.example.json": "run_meta.schema.json",
    "scenario_design.example.json": "scenario_design.schema.json",
    "routed_discharge.example.json": "routed_discharge.schema.json",
    "whatif_request.example.json": "whatif_request.schema.json",
    "whatif_response.example.json": "whatif_response.schema.json",
}


@pytest.mark.parametrize("example_name,schema_name", sorted(EXAMPLE_TO_SCHEMA.items()))
def test_example_matches_schema(example_name, schema_name):
    payload = schemas.load_example(example_name)
    schemas.validate(schema_name, payload)  # raises ContractViolation on mismatch


def test_every_example_file_is_covered():
    on_disk = {p.name for p in schemas.EXAMPLES_DIR.glob("*.json")}
    assert on_disk == set(EXAMPLE_TO_SCHEMA), (
        "contracts/examples/ and this test's EXAMPLE_TO_SCHEMA map have drifted apart"
    )


def test_styles_json_matches_schema():
    import json

    styles = json.loads((schemas.CONTRACTS_DIR / "styles.json").read_text())
    schemas.validate("styles.schema.json", styles)
