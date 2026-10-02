"""Facts read from real GenCase / DualSPHysics logs (Feature 6 provenance).

Every field is taken from text the binaries themselves printed; a field the log does not contain
stays `None` (CLAUDE.md rule 3). Formats were checked against the retained DualSPHysics 5.4.355 /
GenCase 5.4.354.01 logs in `backend/m4_pilot/`.
"""
from __future__ import annotations

import re

_INT = r"([\d,]+)"
_PATTERNS = {
    # "DualSPHysics5 v5.4.355 (08-04-2025)". Three-part version only, so the references section's
    # "DualSPHysics v5.0" citation is never read as the running version.
    "solver_version": re.compile(r"DualSPHysics[\d.]*\s+v(\d+\.\d+\.\d+)"),
    "gencase_version": re.compile(r"GenCase v(\d+\.\d+\.\d+(?:\.\d+)?)"),
    "gpu_name": re.compile(r'Device \d+: "([^"]+)"'),
    "cuda_versions": re.compile(r"CUDA Driver Version / Runtime Version:\s*([\d.]+)\s*/\s*([\d.]+)"),
    "total_particles": re.compile(rf"Total particles:\s*{_INT}\s*\(bound={_INT}.*?fluid={_INT}\)"),
    "initial_particles": re.compile(rf"Particles of simulation \(initial\)\.*:\s*{_INT}"),
    "excluded_particles": re.compile(rf"Excluded particles\.*:\s*{_INT}"),
    "max_particles": re.compile(rf"Maximum number of particles\.*:\s*{_INT}"),
    "part_files": re.compile(rf"PART files\.*:\s*{_INT}"),
    "finished_code": re.compile(r"Finished execution \(code=(-?\d+)\)"),
}
_PART_ROW = re.compile(r"^\d{5}\s+[\d.]+\s+[\d,]+\s+[\d,]+\s+([\d,]+)\s", re.M)
EXCLUSION_WARNING = "More than 100% of current fluid particles were excluded"


def _int(value: str) -> int:
    return int(value.replace(",", ""))


def parse_log(text: str | None) -> dict:
    """Solver/GenCase facts from one log's text. Missing facts are `None`."""
    facts: dict = {k: None for k in ("solver_version", "gencase_version", "gpu_name", "cuda_driver_version",
                                    "cuda_runtime_version", "total_particles", "bound_particles",
                                    "fluid_particles", "initial_particles", "excluded_particles",
                                    "max_particles", "part_files", "final_part_particles", "finished_code")}
    facts["exclusion_warning"] = False
    if not text:
        return facts
    for key in ("solver_version", "gencase_version", "gpu_name"):
        if m := _PATTERNS[key].search(text):
            facts[key] = m.group(1)
    if m := _PATTERNS["cuda_versions"].search(text):
        facts["cuda_driver_version"], facts["cuda_runtime_version"] = m.group(1), m.group(2)
    if m := _PATTERNS["total_particles"].search(text):
        facts["total_particles"], facts["bound_particles"], facts["fluid_particles"] = map(_int, m.groups())
    elif m := re.search(rf"Total particles:\s*{_INT}", text):
        facts["total_particles"] = _int(m.group(1))
    for key in ("initial_particles", "excluded_particles", "max_particles", "part_files"):
        if m := _PATTERNS[key].search(text):
            facts[key] = _int(m.group(1))
    if m := _PATTERNS["finished_code"].search(text):
        facts["finished_code"] = int(m.group(1))
    rows = _PART_ROW.findall(text)
    if rows:
        facts["final_part_particles"] = _int(rows[-1])
    facts["exclusion_warning"] = EXCLUSION_WARNING in text
    return facts


def particle_retention(facts: dict, max_excluded_fraction: float) -> dict:
    """Did the run keep its fluid? `SUCCEEDED` / `FAILED` / `UNAVAILABLE`, with the evidence.

    Fluid that ever existed is at least `max(maximum particles, final particles + excluded)`;
    the excluded fraction uses that as its denominator (an upper bound on the loss fraction).
    Particles leaving through an outlet zone are not "excluded" in DualSPHysics's count.
    """
    excluded, final, peak = facts.get("excluded_particles"), facts.get("final_part_particles"), facts.get("max_particles")
    bound = facts.get("bound_particles") or 0
    out = {"status": "UNAVAILABLE", "excluded_particles": excluded, "final_part_particles": final,
           "max_particles": peak, "bound_particles": bound or None,
           "exclusion_warning": bool(facts.get("exclusion_warning")),
           "max_excluded_fraction": max_excluded_fraction, "excluded_fraction": None,
           "rule": "excluded / max(max_particles, final_part_particles + excluded); "
                   "FAILED on the solver's >100% exclusion warning, a fraction above the limit, or no fluid left"}
    if excluded is None or final is None:
        out["reason"] = "solver log has no excluded-particle summary or PART table"
        return out
    denominator = max(peak or 0, final + excluded)
    out["excluded_fraction"] = excluded / denominator if denominator else None
    final_fluid = final - bound
    out["final_fluid_particles"] = final_fluid
    if out["exclusion_warning"]:
        out.update(status="FAILED", reason="solver warned that more than 100% of current fluid particles were excluded")
    elif out["excluded_fraction"] is not None and out["excluded_fraction"] > max_excluded_fraction:
        out.update(status="FAILED", reason=f"excluded fraction {out['excluded_fraction']:.4f} > {max_excluded_fraction}")
    elif final_fluid <= 0:
        out.update(status="FAILED", reason="no fluid particles remain in the final PART output")
    else:
        out["status"] = "SUCCEEDED"
    return out
