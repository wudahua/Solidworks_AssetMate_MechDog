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


def extract_cylinders(body, call_method, include_face=False):
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
        # 0.8.340: a cylindrical face knows its OWNER FEATURE, and that is a far better
        # hole/bend discriminator than the radius: `OneBend` is a bend fillet, `ICE`
        # (cut-extrude) / `HoleWzd` is a hole. The old radius-delta heuristic paired
        # Ø12/Ø14 concentric holes (Δr = t) as "bends" — 80 cylinders became 36 bends.
        owner_name = owner_type = None
        try:
            owner = win.Dispatch(face.GetFeature)
            owner_name = str(owner.Name)
            owner_type = str(owner.GetTypeName2)
        except Exception:
            pass
        kind = _cylinder_kind(owner_type, owner_name)
        via_pattern = False
        if kind == "pattern" and owner_name is not None:
            # A patterned hole's own feature is `LPattern`; its SOURCE is the cut. Resolve
            # it here, where the feature object is already in hand.
            resolved = resolve_pattern_kind(owner, win.Dispatch, win)
            if resolved:
                kind, via_pattern = resolved, True
        entry = {"point": point, "axis": axis, "radius_mm": round(radius_mm, 3),
                 "owner_name": owner_name, "owner_type": owner_type,
                 "kind": kind, "via_pattern": via_pattern}
        if include_face:
            # Private: the COM face, so callers can walk its edges without a second
            # full GetFaces pass. Never serialised into a receipt.
            entry["_face"] = face
        cylinders.append(entry)
    return cylinders


#: Feature types whose cylindrical faces ARE bend fillets (sheet-metal tooling).
BEND_FEATURE_TYPES = frozenset({
    "OneBend", "MiterFlange", "Hem", "JogBend", "SketchBend", "LoftedBend",
    "SweptFlange", "EdgeFlange", "SheetMetal",
})
#: …and whose cylindrical faces are holes.
HOLE_FEATURE_TYPES = frozenset({
    "HoleWzd", "AdvancedHole", "ICE", "Cut", "ExtrudedCut", "CutExtrude", "CutSweep",
})
#: Ordinary rounds — neither a bend nor a hole.
FILLET_FEATURE_TYPES = frozenset({"Fillet", "Chamfer"})


def _cylinder_kind(owner_type, owner_name):
    """`bend` / `hole` / `fillet` / `pattern` / `other`, from the owning feature. PURE.

    `pattern` is honest rather than guessed: a patterned hole's own feature is
    `LPattern`, so the caller must resolve it through the pattern's source feature.
    """
    t = str(owner_type or "")
    n = str(owner_name or "")
    if t in BEND_FEATURE_TYPES:
        return "bend"
    if t == "ICE":
        # `ICE` covers BOTH cut-extrude and boss-extrude: a Ø3 shaft was counted as a hole
        # and then reported "too close to the bend". The feature NAME is the only
        # discriminator this binding exposes (`GetTypeName2` is the same for both).
        low = n.lower()
        if "cut" in low or "切除" in n:
            return "hole"
        if "boss" in low or "凸台" in n or "拉伸" in n:
            return "other"          # a boss / shaft, not a hole
        return "other"
    if t in HOLE_FEATURE_TYPES:
        return "hole"
    if t in FILLET_FEATURE_TYPES:
        return "fillet"
    if "Pattern" in t or "Mirror" in t:
        return "pattern"
    return "other"


def resolve_pattern_kind(face_owner, call_method, win_dispatch, depth=2):
    """Walk a pattern's parents and return the kind of its source feature, or None. PURE-ish.

    Measured on door.SLDPRT: three cylindrical faces belong to `LPattern1..3`, whose parent
    is a cut-extrude — i.e. patterned holes. Without this they were counted as `pattern`
    and silently dropped from BOTH the bend list and the hole list.
    """
    seen = set()
    stack = [(face_owner, 0)]
    found = set()
    while stack:
        node, level = stack.pop()
        if node is None or level > depth:
            continue
        try:
            key = str(node.Name)
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            parents = list(node.GetParents or ())
        except Exception:
            parents = []
        for raw in parents:
            try:
                parent = win_dispatch(raw)
                kind = _cylinder_kind(parent.GetTypeName2, parent.Name)
            except Exception:
                continue
            if kind in ("bend", "hole", "fillet"):
                found.add(kind)
            else:
                stack.append((parent, level + 1))
    return found.pop() if len(found) == 1 else None


def skew_line_distance(p1, d1, p2, d2):
    """Shortest distance between two 3D lines given a point and a direction each. PURE.

    Returns (distance, kind) where kind is "crossing" (the common perpendicular exists
    inside both segments' infinite lines) or "parallel". Used for hole-axis to bend-axis,
    which is what "hole too close to the bend line" actually means.

    Units follow the inputs (metres here). Returns (None, "degenerate") if a direction is
    zero-length.
    """
    import math as _m

    def _norm(v):
        n = _m.sqrt(sum(float(x) * float(x) for x in v[:3]))
        return None if n <= 1e-12 else [float(x) / n for x in v[:3]]

    u, v = _norm(d1), _norm(d2)
    if u is None or v is None:
        return None, "degenerate"
    w = [float(p2[i]) - float(p1[i]) for i in range(3)]
    cross = [u[1] * v[2] - u[2] * v[1],
             u[2] * v[0] - u[0] * v[2],
             u[0] * v[1] - u[1] * v[0]]
    cnorm = _m.sqrt(sum(c * c for c in cross))
    if cnorm <= 1e-9:                       # parallel
        # distance from p2 to the line (p1, u)
        dot = sum(w[i] * u[i] for i in range(3))
        perp = [w[i] - dot * u[i] for i in range(3)]
        return _m.sqrt(sum(c * c for c in perp)), "parallel"
    return abs(sum(w[i] * cross[i] for i in range(3))) / cnorm, "crossing"


def _line_dir(curve):
    """Direction of an `ICurve` line as a unit vector, or None."""
    import math as _m
    try:
        vals = [float(x) for x in curve.LineParams]
    except Exception:
        return None
    if len(vals) < 6:
        return None
    d = vals[3:6]
    n = _m.sqrt(sum(x * x for x in d))
    return None if n <= 1e-12 else [x / n for x in d]


def _point_of(curve):
    try:
        vals = [float(x) for x in curve.LineParams]
    except Exception:
        return None
    return vals[:3] if len(vals) >= 6 else None


def _perp_distance(p0, axis, q0):
    """Distance from point q0 to the line through p0 with direction axis."""
    import math as _m
    w = [q0[i] - p0[i] for i in range(3)]
    n = _m.sqrt(sum(x * x for x in axis))
    if n <= 1e-12:
        return None
    d = [x / n for x in axis]
    dot = sum(w[i] * d[i] for i in range(3))
    perp = [w[i] - dot * d[i] for i in range(3)]
    return _m.sqrt(sum(x * x for x in perp))


def bend_flange_lengths(bend_face, dispatch, call_method=None):
    """Approximate flange length on BOTH sides of a bend, in mm. Reads geometry only.

    0.8.342 (v0.9 step 2b). The 4t+R rule is about how much material each side of a bend
    has before its free edge. Walk: inner bend face -> its two tangent LINES -> the planar
    face on the other side of each -> every straight edge of that plane -> the largest
    perpendicular distance to the tangent line.

    APPROXIMATE BY CONSTRUCTION (`confidence: "approx"`):
      * an L-shaped or notched flange gives its farthest edge, so the number is an upper
        bound of the width at the worst point, not the minimum width;
      * a plane that spans two bends reports the strip width, which is what the rule wants
        there anyway.

    Returns `{"sides": [{"length_mm": .., "edges_seen": n}], "confidence": "approx"}`.
    Missing pieces are reported as `sides: []` with a `reason`, never guessed.
    """
    axis = None
    try:
        params = [float(v) for v in bend_face.GetSurface.CylinderParams]
        axis = params[3:6]
    except Exception:
        return {"sides": [], "reason": "cylinder params unreadable", "confidence": None}
    try:
        edges = list(bend_face.GetEdges or ())
    except Exception:
        return {"sides": [], "reason": "bend face has no readable edges",
                "confidence": None}

    sides = []
    for raw in edges:
        try:
            edge = dispatch(raw)
            curve = dispatch(edge.GetCurve)
        except Exception:
            continue
        origin = _point_of(curve)
        if origin is None:                # not a line (arc edge of the fillet)
            continue
        try:
            adj = edge.GetTwoAdjacentFaces2
            adj = adj() if callable(adj) else adj
        except Exception:
            adj = None
        for araw in (adj or ()):
            try:
                face = dispatch(araw)
                if not bool(face.GetSurface.IsPlane):
                    continue
                far = 0.0
                seen = 0
                for eraw in (face.GetEdges or ()):
                    try:
                        ec = dispatch(dispatch(eraw).GetCurve)
                    except Exception:
                        continue
                    p = _point_of(ec)
                    if p is None:
                        continue
                    d = _perp_distance(origin, axis, p)
                    if d is None:
                        continue
                    seen += 1
                    far = max(far, d * 1000.0)
                if seen:
                    sides.append({"length_mm": round(far, 2), "edges_seen": seen})
            except Exception:
                continue
    if not sides:
        return {"sides": [], "reason": "no planar neighbour with readable edges",
                "confidence": None}
    return {"sides": sides, "confidence": "approx"}


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
