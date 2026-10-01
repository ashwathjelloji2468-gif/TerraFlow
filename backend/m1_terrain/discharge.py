"""Feature 2: river discharge at the site's inflow point -> `data/<site_id>/raw/discharge_glofas.csv`.

Source (`docs/data_sources.md` src_075): GloFAS v4 daily river discharge (Copernicus Emergency
Management Service, ~0.05 deg / ~5 km), served by the Open-Meteo Flood API
(`https://flood-api.open-meteo.com/v1/flood`, no API key for non-commercial use). The series is
**modelled** (reanalysis, then archived forecasts), not gauged -- every consumer must treat it as
`kind: predicted`/hydrological context, and it is never written into the site config's
`inflow.base_flow` automatically (site configs are owned by the team, CLAUDE.md rule 3).

Point: `domains.far_field.inflow.location` when given, else `dams[0].location` (recorded as
`point_basis`). Output columns: `date,river_discharge_m3s` (UTC dates, SI m^3/s). Provenance holds
the requested and returned grid point, period, gaps and summary statistics.
"""

from __future__ import annotations

import csv
import hashlib
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from backend.shared.site_config import SiteConfig

SOURCE_ID = "src_075"
SERVICE_URL = "https://flood-api.open-meteo.com/v1/flood"
DATASET = "glofas_v4_river_discharge_daily"
LICENSE = "GloFAS (Copernicus EMS) via Open-Meteo, CC BY 4.0, non-commercial API use; see https://open-meteo.com/en/licence"
OUTPUT_NAME = "discharge_glofas.csv"


class DischargeUnavailable(RuntimeError):
    """The discharge series could not be fetched; the message says why."""


def inflow_point(cfg: SiteConfig) -> tuple[float, float, str]:
    loc = cfg.domains.far_field.inflow.location
    if loc.value is not None:
        return float(loc.value[0]), float(loc.value[1]), "domains.far_field.inflow.location"
    dam = cfg.dams[0]
    if dam.location.value is not None:
        return float(dam.location.value[0]), float(dam.location.value[1]), f"dams[0] ({dam.id}).location"
    raise DischargeUnavailable("both domains.far_field.inflow.location and dams[0].location are placeholders (null)")


def _percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def fetch_discharge(cfg: SiteConfig, raw_dir: Path, *, start_date: str = "1984-01-01",
                    end_date: date | None = None, client: httpx.Client | None = None,
                    timeout_s: float = 60.0) -> dict:
    """Download the daily series and return its provenance entry (status `fetched`). Raises
    DischargeUnavailable with an actionable message on any failure (nothing is written then)."""
    lon, lat, basis = inflow_point(cfg)
    end = end_date or (datetime.now(timezone.utc).date() - timedelta(days=1))
    params = {"latitude": lat, "longitude": lon, "daily": "river_discharge",
              "start_date": start_date, "end_date": end.isoformat()}
    owns = client is None
    client = client or httpx.Client(timeout=timeout_s, follow_redirects=True)
    try:
        resp = client.get(SERVICE_URL, params=params)
    except httpx.HTTPError as e:
        raise DischargeUnavailable(f"Open-Meteo Flood API request failed ({type(e).__name__}) for {params}") from None
    finally:
        if owns:
            client.close()
    if resp.status_code != 200:
        raise DischargeUnavailable(f"Open-Meteo Flood API returned HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        body = resp.json()
        times = body["daily"]["time"]
        values = body["daily"]["river_discharge"]
        unit = body.get("daily_units", {}).get("river_discharge")
    except (ValueError, KeyError, TypeError) as e:
        raise DischargeUnavailable(f"unexpected Open-Meteo Flood API response ({e!r})") from None
    if unit not in ("m³/s", "m3/s", "m^3/s"):
        raise DischargeUnavailable(f"unexpected discharge unit {unit!r}; expected m³/s")
    if len(times) != len(values) or not times:
        raise DischargeUnavailable("Open-Meteo Flood API returned an empty or misaligned series")
    valid = [float(v) for v in values if v is not None]
    if not valid:
        raise DischargeUnavailable(f"GloFAS has no discharge values at ({lat}, {lon}) -- not a river cell?")

    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    out = raw_dir / OUTPUT_NAME
    tmp = out.with_suffix(".csv.part")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "river_discharge_m3s"])
        for t, v in zip(times, values):
            w.writerow([t, "" if v is None else v])
    tmp.rename(out)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    return {
        "file": OUTPUT_NAME, "status": "fetched", "dataset": DATASET, "source": SOURCE_ID,
        "service_url": SERVICE_URL, "kind": "modelled (GloFAS reanalysis / archived forecast), not gauged",
        "request_point_lonlat": [lon, lat], "point_basis": basis,
        "returned_point_lonlat": [body.get("longitude"), body.get("latitude")],
        "period": [times[0], times[-1]], "n_days": len(times), "n_missing": len(times) - len(valid),
        "units": "m^3/s", "time_basis": "daily, UTC dates",
        "summary_m3s": {"min": min(valid), "p10": _percentile(valid, 0.10), "median": statistics.median(valid),
                        "mean": statistics.fmean(valid), "p90": _percentile(valid, 0.90), "max": max(valid)},
        "license": LICENSE, "sha256": digest, "size_bytes": out.stat().st_size,
        "fetched_at": datetime.now(timezone.utc).isoformat(), "contract_version": "0.3.0",
    }
