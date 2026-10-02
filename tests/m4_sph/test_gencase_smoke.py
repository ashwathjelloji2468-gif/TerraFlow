"""Run the real DualSPHysics GenCase binary (CPU-only, seconds) on a generated case, skipped
unless `DSPH_BIN_DIR` points at a `bin/linux` (or `bin/windows`) directory. GenCase actually
parsing and accepting the XML is a stronger check than schema validation alone -- it caught two
real bugs during development (missing required constants, and a boundary `mk` outside
`mkconfig`'s declared range). Never runs the DualSPHysics solver itself (rule 14)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from backend.m4_sph.generator import build_nearfield_case, pilot_case_spec, write_case
from backend.m4_sph.case_xml import write_case_xml
from backend.m4_sph.settings import load_sph_settings

DSPH_BIN_DIR = os.environ.get("DSPH_BIN_DIR")
pytestmark = pytest.mark.skipif(not DSPH_BIN_DIR, reason="set DSPH_BIN_DIR to a DualSPHysics bin/ directory to run")


def _run_gencase(case_stem: str, out_prefix: str, cwd: Path) -> subprocess.CompletedProcess:
    gencase = Path(DSPH_BIN_DIR) / "GenCase_linux64"
    env = {**os.environ, "LD_LIBRARY_PATH": DSPH_BIN_DIR}
    return subprocess.run(
        [str(gencase), case_stem, out_prefix, "-save:all"],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )


def test_gencase_accepts_the_pilot_case(tmp_path):
    write_case_xml(pilot_case_spec(), tmp_path / "pilot_Def.xml")
    result = _run_gencase("pilot_Def", "out/pilot", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Total particles: 21,001" in result.stdout


def test_gencase_accepts_a_real_nearfield_case(synth_terrain_dir, synth_hydrograph_params, synth_sites_dir, tmp_path):
    settings = load_sph_settings(dp_m=2.0, t_end_s=60.0)
    spec, case_meta = build_nearfield_case(
        "synth", "synth_s001", synth_hydrograph_params, settings,
        data_dir=synth_terrain_dir.parent.parent, sites_dir=synth_sites_dir,
    )
    case_dir = write_case(spec, case_meta, tmp_path / "run", synth_terrain_dir)

    result = _run_gencase("synth_s001__sph_Def", "out/synth_s001__sph", case_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Exception" not in result.stdout
    assert "Fixed...." in result.stdout  # boundary particles were generated from nearfield.stl
