"""M4 SPH case settings (`config/m4_sph.yaml`) -- numerical/solver knobs, not site facts
(docs/handoff_contract.md rule 3; the contract itself notes M3/M4 simulation settings aren't in
`SiteConfig` yet). Loaded once per call and overridable, the way `config/manning_n.csv` is
project-maintained but not a `SourcedValue`."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from pathlib import Path

import yaml

DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parents[2] / "config" / "m4_sph.yaml"


@dataclass(frozen=True)
class SphSettings:
    dp_m: float | str = "auto"
    t_start_s: float = 0.0
    t_end_s: float | None = None
    time_out_s: float = 5.0
    inlet_width_m: float = 20.0
    inlet_height_m: float = 15.0
    inlet_layers: int = 4
    outlet_margin_m: float = 20.0
    boundary_layers: int = 3
    probe_velocity_height_m: float = 1.0
    vram_budget_mib: float = 8188.0
    vram_margin: float = 0.15
    binaries_dir: str | None = None
    elevation_dz_dp_fraction: float = 0.5
    velocity_levels: int = 3
    surface_interval_s: float = 300.0
    postprocess_row_chunk: int = 200
    max_excluded_particle_fraction: float = 0.05
    routed_section_width_m: float = 300.0
    inlet_terrain_clearance_dp: float = 1.0
    min_inlet_height_dp: float = 3.0

    def __post_init__(self):
        if not (isinstance(self.dp_m, (int, float)) and self.dp_m > 0) and self.dp_m != "auto":
            raise ValueError(f"dp_m must be 'auto' or a positive number, got {self.dp_m!r}")
        if self.t_end_s is not None and self.t_end_s <= self.t_start_s:
            raise ValueError(f"t_end_s ({self.t_end_s}) must be greater than t_start_s ({self.t_start_s})")
        if self.outlet_margin_m < 0.0:
            raise ValueError(f"outlet_margin_m must be >= 0, got {self.outlet_margin_m}")
        if not 0.0 <= self.vram_margin < 1.0:
            raise ValueError(f"vram_margin must be in [0, 1), got {self.vram_margin}")
        if not 0.0 < self.elevation_dz_dp_fraction <= 1.0:
            raise ValueError(f"elevation_dz_dp_fraction must be in (0, 1], got {self.elevation_dz_dp_fraction}")
        if self.velocity_levels < 1:
            raise ValueError(f"velocity_levels must be >= 1, got {self.velocity_levels}")
        if self.surface_interval_s <= 0:
            raise ValueError(f"surface_interval_s must be positive, got {self.surface_interval_s}")
        if not 0.0 <= self.max_excluded_particle_fraction < 1.0:
            raise ValueError(f"max_excluded_particle_fraction must be in [0, 1), got {self.max_excluded_particle_fraction}")
        if self.inlet_terrain_clearance_dp < 1.0:
            raise ValueError(f"inlet_terrain_clearance_dp must be >= 1 (one particle spacing), got {self.inlet_terrain_clearance_dp}")
        if self.min_inlet_height_dp <= 0:
            raise ValueError(f"min_inlet_height_dp must be positive, got {self.min_inlet_height_dp}")
        if self.routed_section_width_m <= 0:
            raise ValueError(f"routed_section_width_m must be positive, got {self.routed_section_width_m}")
        if self.postprocess_row_chunk < 1:
            raise ValueError(f"postprocess_row_chunk must be >= 1, got {self.postprocess_row_chunk}")


def load_sph_settings(path: str | Path | None = None, **overrides) -> SphSettings:
    """Load `config/m4_sph.yaml` (or `path`), applying any keyword overrides on top."""
    path = Path(path) if path is not None else DEFAULT_SETTINGS_PATH
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    known = {f.name for f in fields(SphSettings)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"{path}: unknown setting(s): {', '.join(sorted(unknown))}")
    settings = SphSettings(**raw)
    return replace(settings, **overrides) if overrides else settings
