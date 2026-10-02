"""The PCA + GP emulator itself: fit on a training-run library, predict one
scenario's maps, save/load (`docs/handoff_contract.md` §4.6), and report a
length-scale sensitivity table (`docs/m5_specs.md` §1-§4).

Not in this module (later sessions, per `docs/m5_specs.md` §5-§8 and
`backend/m5_emulator/__init__.py`): LOOCV/`validation/loocv.json`, Monte
Carlo / unknown-breach mode, the confidence rule, `get_flood()`, and the
empirical fallback.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np

from backend.m5_emulator.gp import (
    GPFitResult,
    DEFAULT_N_RESTARTS,
    describe_fitted_gp,
    fit_component_gps,
    predict_components,
)
from backend.m5_emulator.inputs import InputScaler, InputSpec
from backend.m5_emulator.pca import PCABasis, corridor_mask, fit_pca
from backend.m5_emulator.transforms import DEFAULT_TRANSFORMS, OutputTransform, TRANSFORMS_BY_NAME, fill_arrival
from backend.shared.grid import CanonicalGrid, FLOAT_NODATA

CONTRACT_VERSION = "0.3.0"

#: Raw map-stack key (matches synthetic.py / M3-M4 raster names) -> the
#: short name the contract's manifest/file layout uses (§4.6:
#: "outputs: {depth, velocity, arrival}", "pca_<output>.npz").
OUTPUT_SHORT_NAME = {"max_depth": "depth", "max_velocity": "velocity", "arrival_time": "arrival"}
OUTPUT_KEYS = tuple(OUTPUT_SHORT_NAME)  # fit/predict order: depth first (arrival's mask depends on it)


@dataclass(frozen=True)
class EmulatorSettings:
    """Every spec default in one place (docs/m5_specs.md §3-§4), so a real
    site config can override thresholds without touching fit/predict code."""

    variance: float = 0.99                 # PCA: smallest D* reaching this cumulative variance
    wet_m: float = 0.03                    # corridor mask: cells wet above this depth, in any run
    buffer_cells: int = 3                  # corridor mask: dilation buffer
    arrival_m: float = 0.1                 # arrival threshold: first time depth exceeds this
    extent_m: float = 0.3                  # extent ("flooded") threshold; not used by fit/predict yet —
    #                                         carried through for future consumers (M6 impact, extent_class layer)
    n_restarts: int = DEFAULT_N_RESTARTS   # GP optimiser random restarts
    seed: int = 0
    band_z: float = 1.645                  # z-score for the 90% band (P5-P95), spec §5.1

    def to_dict(self) -> dict:
        return {
            "variance": self.variance, "wet_m": self.wet_m, "buffer_cells": self.buffer_cells,
            "arrival_m": self.arrival_m, "extent_m": self.extent_m, "n_restarts": self.n_restarts,
            "seed": self.seed, "band_z": self.band_z,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EmulatorSettings":
        return cls(**d)


@dataclass
class OutputEmulator:
    """One output's (depth | velocity | arrival) fitted transform + PCA +
    per-component GPs."""

    raw_name: str            # "max_depth" | "max_velocity" | "arrival_time"
    transform: OutputTransform
    pca: PCABasis
    gps: list[GPFitResult]
    reconstruction_rmse_transformed: float
    reconstruction_rmse_physical: float
    physical_rmse_unit: str
    physical_rmse_basis: str  # what cells the physical RMSE was computed over

    @property
    def short_name(self) -> str:
        return OUTPUT_SHORT_NAME[self.raw_name]


@dataclass
class EmulatedMaps:
    """One `FloodEmulator.predict()` result: full-grid maps per output, plus
    the latent (PCA-component-space) prediction and which inputs were
    inside the training box."""

    grid: CanonicalGrid
    central: dict[str, np.ndarray]  # raw_name -> (height, width)
    low: dict[str, np.ndarray]
    high: dict[str, np.ndarray]
    latent_mean: dict[str, np.ndarray]  # raw_name -> (n_components,)
    latent_std: dict[str, np.ndarray]
    inputs_raw: np.ndarray
    inside_training_box: dict[str, bool]


def _corridor_indices(mask: np.ndarray) -> np.ndarray:
    return np.flatnonzero(mask.reshape(-1))


def _code_version() -> str | None:
    """Delegates to `backend.shared.version` (Feature 12); `None` when unknown, as before."""
    from backend.shared.version import UNKNOWN, code_version
    v = code_version()
    return None if v == UNKNOWN else v

@dataclass
class FloodEmulator:
    """A trained PCA + GP emulator for one site + model (delft3d | sph |
    synthetic), scenario parameters -> summary maps (docs/m5_specs.md §1)."""

    site_id: str
    model: str
    grid: CanonicalGrid
    input_scaler: InputScaler
    settings: EmulatorSettings
    t_end_s: float
    run_ids: list[str]
    outputs: dict[str, OutputEmulator]  # raw_name -> OutputEmulator
    corridor_mask: np.ndarray           # (height, width) bool, shared across outputs
    trained_at: str
    code_version: str | None

    # ------------------------------------------------------------------ fit

    @classmethod
    def fit(
        cls,
        site_id: str,
        model: str,
        X_raw: np.ndarray,
        maps: dict[str, np.ndarray],  # "max_depth"/"max_velocity"/"arrival_time" -> (N, n_cells)
        grid: CanonicalGrid,
        input_specs: list[InputSpec],
        run_ids: list[str],
        t_end_s: float,
        settings: EmulatorSettings | None = None,
    ) -> "FloodEmulator":
        settings = settings or EmulatorSettings()
        n_runs = X_raw.shape[0]
        # A four-run library is the documented minimum for LOOCV. Each fold
        # trains on N-1 runs, so its PCA/GP fit must accept three observations.
        if n_runs < 3:
            raise ValueError(f"need at least 3 training runs, got {n_runs}")
        for key in OUTPUT_KEYS:
            if maps[key].shape[0] != n_runs:
                raise ValueError(f"maps['{key}'] has {maps[key].shape[0]} rows, X_raw has {n_runs}")
        if len(run_ids) != n_runs:
            raise ValueError(f"run_ids has {len(run_ids)} entries, expected {n_runs}")

        input_scaler = InputScaler.fit(X_raw, input_specs)
        X_std = input_scaler.transform(X_raw)
        input_names = [s.name for s in input_specs]

        mask = corridor_mask(maps["max_depth"], grid.shape, wet_m=settings.wet_m, buffer_cells=settings.buffer_cells)
        idx = _corridor_indices(mask)
        max_components = max(1, n_runs - 2)

        outputs: dict[str, OutputEmulator] = {}
        depth_wet_corridor: np.ndarray | None = None  # (N, n_corridor) bool, built while processing depth

        for raw_name in OUTPUT_KEYS:
            raw_stack = maps[raw_name]
            transform = DEFAULT_TRANSFORMS[raw_name]

            if raw_name == "arrival_time":
                had_arrival = raw_stack[:, idx] != FLOAT_NODATA  # before filling — "truth arrival is defined"
                filled = fill_arrival(raw_stack, t_end_s)
                Z_full = transform.forward(filled)
            else:
                had_arrival = None
                Z_full = transform.forward(raw_stack)

            Z_corridor = Z_full[:, idx]
            raw_corridor = raw_stack[:, idx]

            pca = fit_pca(Z_corridor, variance=settings.variance, max_components=max_components)
            scores = pca.encode(Z_corridor)
            gps = fit_component_gps(X_std, scores, input_names, n_restarts=settings.n_restarts, seed=settings.seed)

            recon_Z = pca.decode(pca.encode(Z_corridor))
            rmse_transformed = float(np.sqrt(np.mean((Z_corridor - recon_Z) ** 2)))
            recon_physical = transform.inverse(recon_Z)

            if raw_name == "max_depth":
                depth_wet_corridor = (raw_corridor > settings.wet_m) | (recon_physical > settings.wet_m)
                wet_mask, unit, basis = depth_wet_corridor, "m", "Omega: wet (>wet_m) in truth or reconstruction"
            elif raw_name == "max_velocity":
                wet_mask, unit, basis = depth_wet_corridor, "m/s", "Omega: depth wet (>wet_m) in truth or reconstruction"
            else:
                wet_mask, unit, basis = had_arrival, "s", "cells where the training run had a real (unfilled) arrival"

            if wet_mask is not None and wet_mask.any():
                diff = recon_physical - raw_corridor
                rmse_physical = float(np.sqrt(np.mean(diff[wet_mask] ** 2)))
            else:
                rmse_physical = float(np.sqrt(np.mean((recon_physical - raw_corridor) ** 2)))

            outputs[raw_name] = OutputEmulator(
                raw_name=raw_name, transform=transform, pca=pca, gps=gps,
                reconstruction_rmse_transformed=rmse_transformed,
                reconstruction_rmse_physical=rmse_physical,
                physical_rmse_unit=unit, physical_rmse_basis=basis,
            )

        return cls(
            site_id=site_id, model=model, grid=grid, input_scaler=input_scaler, settings=settings,
            t_end_s=t_end_s, run_ids=list(run_ids), outputs=outputs, corridor_mask=mask,
            trained_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            code_version=_code_version(),
        )

    # -------------------------------------------------------------- predict

    def predict(self, x_raw: np.ndarray) -> EmulatedMaps:
        """Predict one scenario's central/low/high maps for every output.

        `central` decodes the GP mean; for log1p outputs this is the
        **median** in physical units, not the mean (docs/m5_specs.md §5.1).
        `low`/`high` decode `mean +/- band_z * std` in transformed space,
        back-transformed at the endpoints (spec: "the band is back-transformed
        at its end points") — this is emulator uncertainty only (PCA
        truncation error and breach-parameter uncertainty are not included).
        Depth/velocity are clipped >= 0; arrival is masked to nodata outside
        the predicted wet extent (central depth <= `settings.arrival_m`) and
        outside the training corridor.
        """
        x_raw = np.asarray(x_raw, dtype=float)
        x_std = self.input_scaler.transform(x_raw).reshape(1, -1)
        inside = self.input_scaler.inside_training_box(x_raw)
        inside_map = {spec.name: bool(inside[i]) for i, spec in enumerate(self.input_scaler.specs)}

        latent_mean: dict[str, np.ndarray] = {}
        latent_std: dict[str, np.ndarray] = {}
        for raw_name in OUTPUT_KEYS:
            oe = self.outputs[raw_name]
            mu, sigma = predict_components(oe.gps, x_std)
            latent_mean[raw_name], latent_std[raw_name] = mu[0], sigma[0]

        central, low, high = self.maps_from_latent(latent_mean, latent_std)

        return EmulatedMaps(
            grid=self.grid, central=central, low=low, high=high,
            latent_mean=latent_mean, latent_std=latent_std,
            inputs_raw=x_raw, inside_training_box=inside_map,
        )

    def maps_from_latent(
        self, latent_mean: dict[str, np.ndarray], latent_std: dict[str, np.ndarray] | None = None,
    ) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, np.ndarray]]:
        """PCA-component-space mean (+ optional std) -> full-grid
        (central, low, high) maps for every output, via this emulator's
        fitted transform/PCA basis per output. Factored out of `predict()`
        so LOOCV baselines that skip the GP (`baselines.py`: linear-in-scores
        has no std, nearest-run blending has neither mean-in-latent-space nor
        std) can reuse the exact same decode -> inverse-transform -> clip ->
        arrival-mask pipeline the GP emulator uses, keeping the comparison
        apples-to-apples.

        `latent_std=None` (or an all-zero std) collapses `low`/`high` to
        `central` — used when a baseline has no uncertainty estimate.
        """
        central_corridor: dict[str, np.ndarray] = {}
        low_corridor: dict[str, np.ndarray] = {}
        high_corridor: dict[str, np.ndarray] = {}

        for raw_name in OUTPUT_KEYS:
            oe = self.outputs[raw_name]
            mu = np.asarray(latent_mean[raw_name], dtype=float).reshape(1, -1)
            mu_z = oe.pca.decode(mu)[0]

            if latent_std is not None:
                sigma = np.asarray(latent_std[raw_name], dtype=float).reshape(1, -1)
                sigma_z = oe.pca.decode_std(sigma)[0]
            else:
                sigma_z = np.zeros_like(mu_z)

            c = oe.transform.inverse(mu_z)
            lo = oe.transform.inverse(mu_z - self.settings.band_z * sigma_z)
            hi = oe.transform.inverse(mu_z + self.settings.band_z * sigma_z)
            lo, hi = np.minimum(lo, hi), np.maximum(lo, hi)
            central_corridor[raw_name], low_corridor[raw_name], high_corridor[raw_name] = c, lo, hi

        central = self._embed_and_clip(central_corridor)
        low = self._embed_and_clip(low_corridor)
        high = self._embed_and_clip(high_corridor)
        self._mask_arrival_by_depth(central, low, high)
        return central, low, high

    def maps_from_corridor_physical(self, corridor_values: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Physical-unit corridor values (raw_name -> (n_corridor_cells,), no
        transform/PCA involved) -> full-grid central maps, embedded, clipped
        and arrival-masked exactly like `maps_from_latent`'s `central`. Used
        by the nearest-run baseline (`baselines.py`), which blends training
        runs' physical maps directly and never goes through this emulator's
        PCA basis at all.
        """
        central = self._embed_and_clip(corridor_values)
        self._mask_arrival_by_depth(central)
        return central

    def _embed_and_clip(self, corridor_values: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """One output's corridor-cell physical values -> full-grid array:
        fill non-corridor cells (`t_end_s` for arrival, else `0.0`), place
        the corridor values, clip to the physical valid range."""
        idx = _corridor_indices(self.corridor_mask)
        n_cells = self.grid.width * self.grid.height
        out: dict[str, np.ndarray] = {}
        for raw_name in OUTPUT_KEYS:
            fill_value = self.t_end_s if raw_name == "arrival_time" else 0.0
            full = np.full(n_cells, fill_value, dtype=np.float32)
            full[idx] = corridor_values[raw_name]
            if raw_name in ("max_depth", "max_velocity"):
                full = np.clip(full, 0.0, None)
            else:  # arrival_time: bound to [0, t_end_s] (see maps_from_latent's docstring note)
                full = np.clip(full, 0.0, self.t_end_s)
            out[raw_name] = full.reshape(self.grid.shape)
        return out

    def _mask_arrival_by_depth(self, *map_dicts: dict[str, np.ndarray]) -> None:
        """Mask `arrival_time` to `FLOAT_NODATA` wherever the FIRST dict's
        (`central`'s) depth is at or below `settings.arrival_m` — the
        predicted-dry cells shouldn't report a fabricated arrival, and
        `low`/`high` (if given) share `central`'s wet/dry classification so
        the three bands stay nodata in the same cells."""
        wet = map_dicts[0]["max_depth"] > self.settings.arrival_m
        for maps in map_dicts:
            maps["arrival_time"][~wet] = FLOAT_NODATA

    # -------------------------------------------------------------- persist

    def save(self, out_dir: str | Path) -> Path:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        length_scales: dict[str, dict[str, dict[str, float]]] = {}
        outputs_manifest: dict[str, dict] = {}
        fit_warnings: dict[str, dict[str, list[str]]] = {}

        for raw_name, oe in self.outputs.items():
            short = oe.short_name
            np.savez(
                out_dir / f"pca_{short}.npz",
                **oe.pca.to_dict(),
                corridor_index=_corridor_indices(self.corridor_mask),
                grid_shape=np.array(self.grid.shape),
            )
            joblib.dump([r.gp for r in oe.gps], out_dir / f"gp_{short}.joblib")

            length_scales[short] = {str(j): r.length_scales for j, r in enumerate(oe.gps)}
            fit_warnings[short] = {str(j): r.at_bounds for j, r in enumerate(oe.gps) if r.at_bounds}
            outputs_manifest[short] = {
                "raw_name": raw_name,
                "transform": oe.transform.name,
                "n_components": oe.pca.n_components,
                "variance_explained": float(oe.pca.explained_variance_ratio.sum()),
                "reconstruction_rmse": {
                    "transformed": oe.reconstruction_rmse_transformed,
                    "physical": oe.reconstruction_rmse_physical,
                    "physical_unit": oe.physical_rmse_unit,
                    "physical_basis": oe.physical_rmse_basis,
                },
            }

        manifest = {
            "contract_version": CONTRACT_VERSION,
            "site_id": self.site_id,
            "model": self.model,
            "run_ids": self.run_ids,
            "inputs": self.input_scaler.to_dict(),
            "outputs": outputs_manifest,
            "kernel": "ConstantKernel * Matern(nu=1.5, ARD) + WhiteKernel",
            "length_scales": length_scales,
            "trained_at": self.trained_at,
            "code_version": self.code_version,
            "t_end_s": self.t_end_s,
            "grid": self.grid.model_dump(),
            "settings": self.settings.to_dict(),
            "fit_warnings": fit_warnings,
        }
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return out_dir

    @classmethod
    def load(cls, in_dir: str | Path) -> "FloodEmulator":
        in_dir = Path(in_dir)
        manifest = json.loads((in_dir / "manifest.json").read_text(encoding="utf-8"))

        grid = CanonicalGrid.model_validate(manifest["grid"])
        input_scaler = InputScaler.from_dict(manifest["inputs"])
        settings = EmulatorSettings.from_dict(manifest["settings"])
        input_names = [s.name for s in input_scaler.specs]

        outputs: dict[str, OutputEmulator] = {}
        mask = None
        for short, entry in manifest["outputs"].items():
            raw_name = entry["raw_name"]
            npz = np.load(in_dir / f"pca_{short}.npz")
            pca = PCABasis.from_dict({k: npz[k] for k in ("mean", "components", "explained_variance",
                                                            "explained_variance_ratio", "n_train")})
            if mask is None:
                idx = npz["corridor_index"]
                grid_shape = tuple(int(v) for v in npz["grid_shape"])
                mask = np.zeros(grid_shape[0] * grid_shape[1], dtype=bool)
                mask[idx] = True
                mask = mask.reshape(grid_shape)

            gp_list = joblib.load(in_dir / f"gp_{short}.joblib")
            gps = [describe_fitted_gp(gp, input_names) for gp in gp_list]

            rmse = entry["reconstruction_rmse"]
            outputs[raw_name] = OutputEmulator(
                raw_name=raw_name,
                transform=TRANSFORMS_BY_NAME[entry["transform"]],
                pca=pca, gps=gps,
                reconstruction_rmse_transformed=rmse["transformed"],
                reconstruction_rmse_physical=rmse["physical"],
                physical_rmse_unit=rmse["physical_unit"],
                physical_rmse_basis=rmse["physical_basis"],
            )

        return cls(
            site_id=manifest["site_id"], model=manifest["model"], grid=grid, input_scaler=input_scaler,
            settings=settings, t_end_s=manifest["t_end_s"], run_ids=manifest["run_ids"], outputs=outputs,
            corridor_mask=mask, trained_at=manifest["trained_at"], code_version=manifest["code_version"],
        )

    # --------------------------------------------------------- sensitivity

    def sensitivity_table(self) -> dict[str, dict]:
        """Fitted length scales per (output, component) plus a per-output
        relative-sensitivity summary: `sum_j evr_j / l_ij`, normalised to sum
        to 1 over inputs. Shorter length scale = the GP needs a smaller move
        in that input to change its prediction, so lower l -> higher weight."""
        input_names = [s.name for s in self.input_scaler.specs]
        table: dict[str, dict] = {}
        for raw_name, oe in self.outputs.items():
            components = [
                {"index": j, "variance_explained": float(oe.pca.explained_variance_ratio[j]),
                 "length_scales": r.length_scales, "noise_level": r.noise_level}
                for j, r in enumerate(oe.gps)
            ]
            raw = np.zeros(len(input_names))
            for j, r in enumerate(oe.gps):
                evr = oe.pca.explained_variance_ratio[j]
                for i, name in enumerate(input_names):
                    raw[i] += evr / r.length_scales[name]
            total = raw.sum()
            relative = {name: float(raw[i] / total) if total > 0 else 0.0 for i, name in enumerate(input_names)}
            table[oe.short_name] = {"components": components, "relative_sensitivity": relative}
        return table

    def format_sensitivity_table(self) -> str:
        """Plain-text rendering of `sensitivity_table()`, one block per
        output: a row per component's length scales, then the relative
        sensitivity summary."""
        input_names = [s.name for s in self.input_scaler.specs]
        table = self.sensitivity_table()
        lines: list[str] = []
        header = "component  variance_expl  " + "  ".join(f"{n:>18s}" for n in input_names)
        for output_name, entry in table.items():
            lines.append(f"\n[{output_name}]")
            lines.append(header)
            for c in entry["components"]:
                ls = "  ".join(f"{c['length_scales'][n]:18.4f}" for n in input_names)
                lines.append(f"{c['index']:>9d}  {c['variance_explained']:13.4f}  {ls}")
            rel = "  ".join(f"{entry['relative_sensitivity'][n]:18.4f}" for n in input_names)
            lines.append(f"{'relative':>9s}  {'':>13s}  {rel}")
        return "\n".join(lines)
