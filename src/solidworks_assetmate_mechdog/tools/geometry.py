"""Geometry helpers for sheet-metal checks: find bends, holes, and distances.

Why this exists: the previous `sheet_metal_qc` could only answer questions the feature tree
exposes (thickness, bend count, relief by name). Minimum flange length and hole-to-bend
distance need real geometry, so they were reported `not_checked`.

The key insight came from measuring cylindrical-face radii on two known parts:

    panel box:  R2.54 x12  +  R3.54 x12   (difference 1.0 = board thickness)
    door:       R2.54 x4   +  R3.54 x4    (same difference)

A sheet-metal bend shows up as TWO coaxial cylindrical faces whose radii differ by exactly
the board thickness — the inner and outer bend radii. Holes show up as isolated cylinders
that do not pair up that way.

So bends can be found without the (unreadable) SheetMetal feature data: pair cylinders by
"coaxial + radius delta == thickness".
"""
from __future__ import annotations

import math

AXIS_EPS = 0.02        # mm tolerance on radius-difference matching (relative to thickness)
CONCENTRIC_EPS = 0.30  # mm tolerance for two axes to be considered the same line


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _norm(a):
    length = math.sqrt(_dot(a, a))
    if length < 1e-12:
        return (0.0, 0.0, 0.0)
    return (a[0] / length, a[1] / length, a[2] / length)


def line_distance(p1, d1, p2, d2):
    """Shortest distance between two infinite lines (skew/parallel aware)."""
    d1n, d2n = _norm(d1), _norm(d2)
    cross = _cross(d1n, d2n)
    cross_len = math.sqrt(_dot(cross, cross))
    delta = _sub(p2, p1)
    if cross_len < 1e-9:
        # Parallel: distance from p2 to line 1
        proj = _dot(delta, d1n)
        closest = (p1[0] + d1n[0] * proj, p1[1] + d1n[1] * proj, p1[2] + d1n[2] * proj)
        return math.sqrt(_dot(_sub(p2, closest), _sub(p2, closest)))
    return abs(_dot(delta, cross)) / cross_len


def point_line_distance(point, line_point, line_dir):
    d = _norm(line_dir)
    delta = _sub(point, line_point)
    proj = _dot(delta, d)
    closest = (line_point[0] + d[0] * proj, line_point[1] + d[1] * proj, line_point[2] + d[2] * proj)
    return math.sqrt(_dot(_sub(point, closest), _sub(point, closest)))


def extract_cylinders(body, call_method):
    """Return every cylindrical face on a body as (point, axis, radius_mm).

    Uses `IBody2.GetFaces` (a method — attribute access raises) and
    `ISurface.CylinderParams`, which is [px,py,pz, dx,dy,dz, radius] in metres.
    """
    import win32com.client as win

    cylinders = []
    try:
        faces = list(call_method(body, "GetFaces") or ())
    except Exception:
        return cylinders
    for raw in faces:
        try:
            face = win.Dispatch(raw)
            surface = face.GetSurface
        except Exception:
            continue
        if surface is None:
            continue
        try:
            if not bool(surface.IsCylinder):
                continue
            params = [float(v) for v in surface.CylinderParams]
        except Exception:
            continue
        if len(params) < 7:
            continue
        point = (params[0], params[1], params[2])
        axis = (params[3], params[4], params[5])
        radius_mm = params[6] * 1000.0
        if radius_mm <= 0:
            continue
        cylinders.append({"point": point, "axis": axis, "radius_mm": round(radius_mm, 3)})
    return cylinders


def pair_bends(cylinders, thickness_mm, radius_eps_ratio=0.25, concentric_eps=CONCENTRIC_EPS,
               max_inner_radius_ratio=3.0):
    """Find sheet-metal bends: coaxial cylinder pairs whose radii differ by the thickness.

    The radius-delta test alone is not enough — on the panel box, Ø12 holes (R6) paired
    with Ø14 holes (R7) also differ by exactly 1.0 mm and were reported as bends, which
    turned 80 cylinders into 36 "bends" and left only 8 holes.

    A bend's inner radius is bounded by forming practice: R_in <= ~3t. That single extra
    condition removes the hole pairs (R6 = 6t) while keeping the real bends (R2.54 = 2.54t).

    Returns (bends, holes). A bend's line is the pair's shared axis; holes are the
    cylinders that did not pair up.
    """
    used = set()
    bends = []
    eps = max(0.05, thickness_mm * radius_eps_ratio)
    max_inner = thickness_mm * max_inner_radius_ratio

    for i in range(len(cylinders)):
        if i in used:
            continue
        for j in range(i + 1, len(cylinders)):
            if j in used:
                continue
            a, b = cylinders[i], cylinders[j]
            delta = abs(a["radius_mm"] - b["radius_mm"])
            if abs(delta - thickness_mm) > eps:
                continue
            # Bend inner radius cannot exceed forming limits; larger pairs are nested holes.
            if min(a["radius_mm"], b["radius_mm"]) > max_inner:
                continue
            # Coaxial? Same direction and the axes pass close to each other.
            parallel = abs(abs(_dot(_norm(a["axis"]), _norm(b["axis"]))) - 1.0) < 1e-3
            if not parallel:
                continue
            if line_distance(a["point"], a["axis"], b["point"], b["axis"]) > concentric_eps:
                continue
            used.add(i)
            used.add(j)
            bends.append({
                "point": a["point"],
                "axis": a["axis"],
                "inner_radius_mm": round(min(a["radius_mm"], b["radius_mm"]), 3),
                "outer_radius_mm": round(max(a["radius_mm"], b["radius_mm"]), 3),
                "radius_mm": round(min(a["radius_mm"], b["radius_mm"]), 3),
            })
            break

    holes = [
        {"point": c["point"], "axis": c["axis"], "radius_mm": c["radius_mm"]}
        for index, c in enumerate(cylinders) if index not in used
    ]
    return bends, holes


def hole_to_bend_report(holes, bends):
    """Distance from every hole axis to its nearest bend line.

    A hole whose axis passes through the bend region is reported with distance 0 — that is
    the "hole on the bend line" case, which cracks when formed.
    """
    rows = []
    for hole in holes:
        best = None
        for index, bend in enumerate(bends):
            distance = line_distance(hole["point"], hole["axis"], bend["point"], bend["axis"])
            if best is None or distance < best[0]:
                best = (distance, index)
        if best is None:
            continue
        rows.append({
            "hole_diameter_mm": round(hole["radius_mm"] * 2, 3),
            "distance_to_bend_mm": round(best[0], 3),
            "bend_index": best[1],
            "bend_radius_mm": bends[best[1]]["radius_mm"],
        })
    rows.sort(key=lambda r: r["distance_to_bend_mm"])
    return rows


def flange_length_report(bends, cylinders, thickness_mm):
    """Approximate minimum flange: distance from each bend line to the nearest cylinder
    that is NOT a bend pair.

    This is a proxy, not a true flange measurement: a real minimum flange is the straight
    run from the bend tangent to the sheet edge, which needs the outer edges of the part.
    What this reports is "how close is the nearest hole to this bend", which is the more
    actionable number anyway.
    """
    if not bends:
        return []
    rows = []
    for index, bend in enumerate(bends):
        nearest = None
        for c in cylinders:
            d = line_distance(c["point"], c["axis"], bend["point"], bend["axis"])
            if d > 1e-6 and (nearest is None or d < nearest[0]):
                nearest = (d, c["radius_mm"])
        rows.append({
            "bend_index": index,
            "bend_radius_mm": bend["radius_mm"],
            "nearest_feature_distance_mm": round(nearest[0], 3) if nearest else None,
            "nearest_feature_diameter_mm": round(nearest[1] * 2, 3) if nearest else None,
        })
    return rows
