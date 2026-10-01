"""Kernel-free check that a generated D-Flow FM net offers a hydraulic path from the breach source
to the downstream outlet (Feature 5).

The case uses `BedLevType = 3`: bed levels live at mesh nodes, a face's bed is the mean of its
nodes, and a flow link between two faces is bounded by its shared edge's two node levels. Water
leaving face `a` across edge `ab` must rise above that edge's higher node (the edge sill). The
"required ponding" of a link is `max(0, sill_ab - bed_a)`; the path cost is the largest required
ponding along it, minimised over all source-to-outlet paths (a minimax / bottleneck path).

A large bottleneck means the mesh cannot carry water from source to outlet without a sizeable
pond behind an edge sill -- e.g. a V-shaped thalweg too narrow for the mesh, where every
down-valley edge has one node up the valley wall. This reads only the net file and outlet
polyline; it does not simulate anything.
"""
from __future__ import annotations

import heapq
from pathlib import Path

import numpy as np


def _faces(net: Path):
    import xarray as xr

    with xr.open_dataset(net) as ds:
        nx = np.asarray(ds["mesh2d_node_x"].values, dtype=float)
        ny = np.asarray(ds["mesh2d_node_y"].values, dtype=float)
        nz = np.asarray(ds["mesh2d_node_z"].values, dtype=float)
        raw = np.asarray(ds["mesh2d_face_nodes"].values, dtype=float)
        start = int(ds["mesh2d_face_nodes"].attrs.get("start_index", 0))
    faces = [tuple(int(v) - start for v in row if np.isfinite(v) and v >= start) for row in raw]
    return nx, ny, nz, faces


def _read_pli(pli: Path) -> list[tuple[float, float]]:
    rows = []
    for line in pli.read_text(errors="replace").splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 2:
            rows.append((float(parts[0]), float(parts[1])))
    return rows


def source_outlet_bottleneck(net: str | Path, source_xy: tuple[float, float], outlet_pli: str | Path) -> dict:
    """Minimax required ponding (m) from the face containing the source to any outlet face."""
    from shapely.geometry import LineString, Point, Polygon

    nx, ny, nz, faces = _faces(Path(net))
    bed = np.array([nz[list(f)].mean() for f in faces])
    polys = [Polygon([(nx[n], ny[n]) for n in f]) for f in faces]
    src_pt = Point(source_xy)
    src = next((i for i, p in enumerate(polys) if p.covers(src_pt)), None)
    if src is None:
        src = int(np.argmin([p.distance(src_pt) for p in polys]))
    outlet = LineString(_read_pli(Path(outlet_pli))).buffer(1.0)
    targets = {i for i, p in enumerate(polys) if p.intersects(outlet)}

    edges: dict[tuple[int, int], list[int]] = {}
    for i, f in enumerate(faces):
        for a, b in zip(f, f[1:] + f[:1]):
            edges.setdefault((min(a, b), max(a, b)), []).append(i)
    adjacency: dict[int, list[tuple[int, float]]] = {}
    for (a, b), owners in edges.items():
        if len(owners) == 2:
            sill = max(nz[a], nz[b])
            i, j = owners
            adjacency.setdefault(i, []).append((j, sill))
            adjacency.setdefault(j, []).append((i, sill))

    best = {src: 0.0}
    prev: dict[int, tuple[int, float]] = {}
    heap = [(0.0, src)]
    reached = None
    while heap:
        cost, i = heapq.heappop(heap)
        if cost > best.get(i, np.inf):
            continue
        if i in targets:
            reached = i
            break
        for j, sill in adjacency.get(i, []):
            c = max(cost, max(0.0, sill - bed[i]))
            if c < best.get(j, np.inf):
                best[j], prev[j] = c, (i, sill)
                heapq.heappush(heap, (c, j))
    if reached is None:
        return {"connected": False, "source_face": src, "outlet_faces": len(targets)}
    # The link that sets the bottleneck.
    worst, node = None, reached
    while node in prev:
        i, sill = prev[node]
        need = max(0.0, sill - bed[i])
        if worst is None or need > worst["required_ponding_m"]:
            worst = {"from_face": i, "to_face": node, "edge_sill_m": float(sill), "from_bed_m": float(bed[i]),
                     "required_ponding_m": float(need)}
        node = i
    return {"connected": True, "source_face": src, "outlet_faces": len(targets),
            "bottleneck_required_ponding_m": float(best[reached]), "bottleneck_link": worst,
            "rule": "BedLevType 3: edge sill = higher shared-edge node; ponding = max(0, sill - face bed)"}
