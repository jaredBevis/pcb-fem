# SPDX-License-Identifier: LGPL-2.1-or-later
"""KiCad .kicad_pcb reader: s-expression parser, stackup, nets, pads, vias, zones.
Pure Python - no FreeCAD needed (used by --dry-run / --list-nets)."""
from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Sequence, Tuple

# ----------------------------------------------------------------------------------
# Options
# ----------------------------------------------------------------------------------


@dataclass
class Options:
    plating: float = 0.025            # mm, barrel wall thickness for drilled holes
    microvia_solid: bool = True       # model microvias as solid copper cylinders
    filled_vias_solid: bool = True    # vias with (filling yes) -> solid barrel
    include_tracks: bool = True
    include_pads: bool = True
    include_vias: bool = True
    include_zones: bool = True
    include_graphics: bool = True
    default_copper: float = 0.035     # mm, used when stackup has no copper thickness
    copper_override: Dict[str, float] = field(default_factory=dict)  # layer -> mm
    arc_segments: int = 24            # discretisation of arcs inside polygons
    refine: bool = True               # removeSplitter() on results
    as_body: bool = True              # PartDesign::Body (True) or Part::Feature
    min_feature: float = 1e-4         # mm, ignore degenerate geometry below this
    poly_tol: float = 1e-3            # mm, polygon cleanup: merge vertices / drop collinear
                                      # points closer than this (KiCad fills contain 1 nm
                                      # edges that make Gmsh produce flat, degenerate tets)


# ----------------------------------------------------------------------------------
# S-expression parser
# ----------------------------------------------------------------------------------

_TOKEN_RE = re.compile(r'\(|\)|"((?:[^"\\]|\\.)*)"|[^\s()"]+', re.S)


class QStr(str):
    """A string that was quoted in the file (distinguishes "1" from 1)."""
    __slots__ = ()


def parse_sexpr(text: str):
    stack: List[list] = [[]]
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok == "(":
            stack.append([])
        elif tok == ")":
            node = stack.pop()
            stack[-1].append(node)
        elif m.group(1) is not None:
            s = m.group(1)
            if "\\" in s:
                s = s.replace('\\"', '"').replace("\\\\", "\\")
            stack[-1].append(QStr(s))
        else:
            stack[-1].append(tok)
    if len(stack) != 1 or not stack[0]:
        raise ValueError("Malformed s-expression (unbalanced parentheses)")
    return stack[0][0]


def is_node(x) -> bool:
    return isinstance(x, list) and len(x) > 0 and isinstance(x[0], str)


def find(node, key):
    for c in node[1:]:
        if is_node(c) and c[0] == key:
            return c
    return None


def findall(node, key):
    return [c for c in node[1:] if is_node(c) and c[0] == key]


def val(node, key, default=None, idx=1):
    c = find(node, key)
    if c is None or len(c) <= idx:
        return default
    return c[idx]


def fval(node, key, default=None, idx=1):
    v = val(node, key, None, idx)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def atoms(node):
    return [c for c in node[1:] if isinstance(c, str) and not isinstance(c, QStr)]


def flag(node, key) -> bool:
    """KiCad flags appear either as bare atoms `(via blind ...)` or as `(key yes)`."""
    if key in atoms(node):
        return True
    c = find(node, key)
    if c is None:
        return False
    if len(c) == 1:
        return True
    return str(c[1]).lower() in ("yes", "true", "1")


def xy(node, key="at"):
    c = find(node, key)
    if c is None:
        return None
    return float(c[1]), float(c[2])


# ----------------------------------------------------------------------------------
# Geometry helpers (pure python)
# ----------------------------------------------------------------------------------

Pt = Tuple[float, float]


def k2f(p: Pt) -> Pt:
    """KiCad (y down) -> FreeCAD (y up)."""
    return (p[0], -p[1])


def rot(p: Pt, deg: float) -> Pt:
    if not deg:
        return p
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return (p[0] * c - p[1] * s, p[0] * s + p[1] * c)


def add(a: Pt, b: Pt) -> Pt:
    return (a[0] + b[0], a[1] + b[1])


def circumcenter(a: Pt, b: Pt, c: Pt) -> Optional[Pt]:
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    if abs(d) < 1e-12:
        return None
    ux = ((a[0] ** 2 + a[1] ** 2) * (b[1] - c[1]) + (b[0] ** 2 + b[1] ** 2) * (c[1] - a[1])
          + (c[0] ** 2 + c[1] ** 2) * (a[1] - b[1])) / d
    uy = ((a[0] ** 2 + a[1] ** 2) * (c[0] - b[0]) + (b[0] ** 2 + b[1] ** 2) * (a[0] - c[0])
          + (c[0] ** 2 + c[1] ** 2) * (b[0] - a[0])) / d
    return (ux, uy)


def arc_points(a: Pt, m: Pt, b: Pt, nseg: int) -> List[Pt]:
    """Discretise a 3-point arc a->m->b (inclusive of both ends)."""
    c = circumcenter(a, m, b)
    if c is None:
        return [a, b]
    r = math.hypot(a[0] - c[0], a[1] - c[1])
    t0 = math.atan2(a[1] - c[1], a[0] - c[0])
    tm = math.atan2(m[1] - c[1], m[0] - c[0])
    t1 = math.atan2(b[1] - c[1], b[0] - c[0])

    def norm(t):
        while t < 0:
            t += 2 * math.pi
        return t
    sweep = norm(t1 - t0)
    if norm(tm - t0) > sweep:          # mid not on CCW path -> go CW
        sweep = sweep - 2 * math.pi
    n = max(2, int(abs(sweep) / (2 * math.pi) * nseg * 2) + 1)
    return [(c[0] + r * math.cos(t0 + sweep * i / n), c[1] + r * math.sin(t0 + sweep * i / n))
            for i in range(n + 1)]


def signed_area(loop: Sequence[Pt]) -> float:
    s = 0.0
    n = len(loop)
    for i in range(n):
        x1, y1 = loop[i]
        x2, y2 = loop[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return 0.5 * s


def point_in_poly(p: Pt, poly: Sequence[Pt]) -> bool:
    x, y = p
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            xc = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < xc:
                inside = not inside
        j = i
    return inside


def _rdp(pts: Sequence[Pt], tol: float) -> List[Pt]:
    """Ramer-Douglas-Peucker on an open chain (keeps both ends); iterative."""
    n = len(pts)
    if n < 3:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        ax, ay = pts[i]
        bx, by = pts[j]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        best, bi = -1.0, -1
        for k in range(i + 1, j):
            px, py = pts[k]
            d = (abs(dx * (ay - py) - (ax - px) * dy) / L) if L > 0 else math.hypot(px - ax, py - ay)
            if d > best:
                best, bi = d, k
        if bi >= 0 and best > tol:
            keep[bi] = True
            stack.append((i, bi))
            stack.append((bi, j))
    return [p for p, k in zip(pts, keep) if k]


def clean_loop(loop: Sequence[Pt], tol: float = 1e-3) -> List[Pt]:
    """
    Remove micro-geometry from a closed polygon so no edge is shorter than ~`tol` and every
    original vertex stays within `tol` of the result (no drift along arcs):
      1. merge consecutive vertices closer than `tol`;
      2. Ramer-Douglas-Peucker with tolerance `tol` (drops near-collinear vertices).
    """
    if tol <= 0 or len(loop) < 3:
        return list(loop)
    pts: List[Pt] = []
    for p in loop:
        if not pts or math.hypot(p[0] - pts[-1][0], p[1] - pts[-1][1]) >= tol:
            pts.append(p)
    while len(pts) > 3 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < tol:
        pts.pop()
    if len(pts) < 3:
        return []
    # split the ring at vertex 0 and the vertex farthest from it, simplify both chains
    far = max(range(len(pts)), key=lambda k: (pts[k][0] - pts[0][0]) ** 2 + (pts[k][1] - pts[0][1]) ** 2)
    a = _rdp(pts[:far + 1], tol)
    b = _rdp(pts[far:] + [pts[0]], tol)
    out = a[:-1] + b[:-1]
    # a merge pass can leave a last pair closer than tol after RDP: repeat step 1 once
    res: List[Pt] = []
    for p in out:
        if not res or math.hypot(p[0] - res[-1][0], p[1] - res[-1][1]) >= tol:
            res.append(p)
    if len(res) > 3 and math.hypot(res[0][0] - res[-1][0], res[0][1] - res[-1][1]) < tol:
        res.pop()
    return res if len(res) >= 3 and abs(signed_area(res)) > tol * tol else []


def unfracture(pts: Sequence[Pt], ndigits: int = 6) -> List[Tuple[List[Pt], List[List[Pt]]]]:
    """
    KiCad stores zone fills as "fractured" polygons: a single outline in which every
    hole is connected to the outer boundary by a zero-width slit (an edge traversed
    once in each direction). Remove those slit edges and rebuild the outer boundary
    and the holes. Returns [(outer_loop, [hole_loops...]), ...].
    """
    def key(p):
        return (round(p[0], ndigits), round(p[1], ndigits))

    n = len(pts)
    edges = []
    for i in range(n):
        a, b = key(pts[i]), key(pts[(i + 1) % n])
        if a != b:
            edges.append((a, b))
    count: Dict[Tuple, int] = defaultdict(int)
    for e in edges:
        r = (e[1], e[0])
        if count[r] > 0:
            count[r] -= 1
        else:
            count[e] += 1
    out_edges: Dict[Pt, List[Pt]] = defaultdict(list)
    for (a, b), c in count.items():
        for _ in range(c):
            out_edges[a].append(b)

    loops: List[List[Pt]] = []
    while True:
        start = next((k for k, v in out_edges.items() if v), None)
        if start is None:
            break
        loop = [start]
        cur = start
        guard = 0
        while True:
            nxt = out_edges[cur].pop()
            if nxt == start:
                break
            loop.append(nxt)
            cur = nxt
            guard += 1
            if not out_edges[cur] or guard > 10 ** 7:
                break
        if len(loop) >= 3 and abs(signed_area(loop)) > 1e-10:
            loops.append(loop)

    if not loops:
        return []
    # nesting depth via even-odd containment
    def probe(loop):
        a, b = loop[0], loop[1]
        return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)

    info = []
    for i, L in enumerate(loops):
        p = probe(L)
        parents = [j for j, M in enumerate(loops) if j != i and point_in_poly(p, M)]
        info.append(parents)
    result = []
    for i, L in enumerate(loops):
        if len(info[i]) % 2 == 0:
            holes = [loops[j] for j in range(len(loops))
                     if i in info[j] and len(info[j]) == len(info[i]) + 1]
            result.append((L, holes))
    return result


# ----------------------------------------------------------------------------------
# Board model (FreeCAD-free)
# ----------------------------------------------------------------------------------


@dataclass
class CuLayer:
    name: str
    thickness: float
    z_bot: float = 0.0
    z_top: float = 0.0
    index: int = 0


@dataclass
class Stackup:
    layers: List[CuLayer]
    dielectrics: List[Tuple[str, float, str]]      # (name, thickness, material)
    total: float
    from_file: bool

    def __post_init__(self):
        self.by_name = {l.name: l for l in self.layers}

    def span(self, la: str, lb: str) -> List[CuLayer]:
        ia, ib = self.by_name[la].index, self.by_name[lb].index
        lo, hi = min(ia, ib), max(ia, ib)
        return self.layers[lo:hi + 1]

    def table(self) -> str:
        rows = ["  %-8s  t=%.4f mm   z=[%.4f, %.4f]" % (l.name, l.thickness, l.z_bot, l.z_top)
                for l in self.layers]
        src = "board stackup" if self.from_file else "DEFAULT (no stackup in file)"
        return "Copper stackup (%s), total %.4f mm:\n%s" % (src, self.total, "\n".join(rows))


def _cu_sort_key(name: str):
    if name == "F.Cu":
        return 0
    if name == "B.Cu":
        return 10 ** 6
    m = re.match(r"In(\d+)\.Cu", name)
    return int(m.group(1)) if m else 10 ** 5


def build_stackup(board, opts: Options) -> Stackup:
    lay_node = find(board, "layers")
    cu_names = []
    if lay_node:
        for l in lay_node[1:]:
            if isinstance(l, list) and len(l) >= 2 and str(l[1]).endswith(".Cu"):
                cu_names.append(str(l[1]))
    cu_names = sorted(set(cu_names), key=_cu_sort_key)
    general_th = fval(find(board, "general") or ["general"], "thickness", 1.6)

    setup = find(board, "setup")
    st = find(setup, "stackup") if setup else None
    seq = []   # top->bottom list of ('cu', name, t) / ('di', name, t, material)
    if st:
        for ln in findall(st, "layer"):
            name = str(ln[1])
            typ = str(val(ln, "type", "")).lower()
            th = sum(float(c[1]) for c in findall(ln, "thickness") if len(c) > 1)
            if name.endswith(".Cu") or typ == "copper":
                seq.append(("cu", name, th))
            elif any(k in typ for k in ("core", "prepreg", "dielectric")) or name.startswith("dielectric"):
                seq.append(("di", name, th, str(val(ln, "material", ""))))

    from_file = bool(seq) and [s[1] for s in seq if s[0] == "cu"] == cu_names
    if not from_file:
        # synthesize: equal dielectrics between copper layers
        n = len(cu_names)
        t_cu = opts.default_copper
        t_di = max(general_th - n * t_cu, 0.1) / max(n - 1, 1)
        seq = []
        for i, nm in enumerate(cu_names):
            seq.append(("cu", nm, t_cu))
            if i < n - 1:
                seq.append(("di", "dielectric %d" % (i + 1), t_di, "FR4"))

    total = 0.0
    for s in seq:
        if s[0] == "cu":
            t = opts.copper_override.get(s[1], s[2] if s[2] > 0 else opts.default_copper)
            total += t
        else:
            total += s[2]
    z = total
    layers, diel = [], []
    for s in seq:
        if s[0] == "cu":
            t = opts.copper_override.get(s[1], s[2] if s[2] > 0 else opts.default_copper)
            layers.append(CuLayer(s[1], t, z - t, z, len(layers)))
            z -= t
        else:
            diel.append((s[1], s[2], s[3]))
            z -= s[2]
    return Stackup(layers, diel, total, from_file)


# --- 2D primitive descriptions (consumed by the FreeCAD back end) -------------------
# All coordinates are already in FreeCAD XY (mm).
#   ("seg",  p0, p1, width)
#   ("arc",  p0, pm, p1, width)
#   ("disk", center, radius)
#   ("ring", center, r_outer, r_inner)               outline circle with width
#   ("poly", outer_pts, [hole_pts...], inflate)      inflate = stroke width/2 (0 = none)
#   ("pad",  shape, center, size(w,h), angle, extras)  extras: dict
#   ("slot", center, size(w,h), angle)               stadium (round hole if w == h)


@dataclass
class Hole:
    center: Pt
    size: Tuple[float, float]        # (w, h), w == h for round
    angle: float
    top: str                          # copper layer names spanned
    bot: str
    plated: bool
    solid: bool                       # microvia / filled -> solid barrel
    kind: str                         # "pth", "via", "blind", "buried", "micro", "npth"
    ref: str = ""


@dataclass
class NetGeom:
    name: str
    layers: Dict[str, list] = field(default_factory=lambda: defaultdict(list))
    holes: List[Hole] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    routed: set = field(default_factory=set)     # layers with tracks/zones/graphics
    terminals: List["TermCandidate"] = field(default_factory=list)  # pads/vias usable as BCs
    warnings: List[str] = field(default_factory=list)


@dataclass
class TermCandidate:
    """A pad or via that can carry an FEM terminal (reachable from an outer copper layer)."""
    kind: str                       # "pad" | "via"
    label: str                      # "R1.1", "U3.EP#2", "VIA(114.000,116.000)"
    sides: Dict[str, tuple]         # "top"/"bottom" -> 2D primitive of the pad on that layer
    layers: Dict[str, str]          # "top"/"bottom" -> copper layer name
    default_side: str
    center: Pt                      # FC coordinates of the pad shape centre
    hole: Optional[Hole] = None
    info: str = ""

    def key(self, side: str) -> str:
        return "%s@%s" % (self.label, side)


class Board:
    def __init__(self, path: str, opts: Optional[Options] = None):
        self.path = path
        self.opts = opts or Options()
        with open(path, "r", encoding="utf-8") as f:
            self.root = parse_sexpr(f.read())
        if self.root[0] != "kicad_pcb":
            raise ValueError("%s is not a kicad_pcb file" % path)
        self.version = int(val(self.root, "version", 0) or 0)
        self.warnings: List[str] = []
        if self.version and self.version < 20211014:
            self.warnings.append("File version %d predates KiCad 6; re-save the board in a "
                                 "modern KiCad for reliable results." % self.version)
        self.stackup = build_stackup(self.root, self.opts)
        self.cu = [l.name for l in self.stackup.layers]
        self.net_by_id: Dict[str, str] = {}
        for n in findall(self.root, "net"):
            if len(n) >= 3:
                self.net_by_id[str(n[1])] = str(n[2])
        self._names = None

    # -- nets -----------------------------------------------------------------------
    def item_net(self, node) -> str:
        n = find(node, "net")
        if n is None:
            nn = val(node, "net_name")
            return str(nn) if nn else ""
        if len(n) >= 3:
            return str(n[2])
        v = n[1]
        if not isinstance(v, QStr) and str(v) in self.net_by_id:
            return self.net_by_id[str(v)]
        return str(v)

    def net_names(self) -> List[str]:
        if self._names is None:
            names = set(v for v in self.net_by_id.values() if v)
            for node in self._iter_items():
                nm = self.item_net(node)
                if nm:
                    names.add(nm)
            self._names = sorted(names)
        return self._names

    def _iter_items(self):
        for c in self.root[1:]:
            if not is_node(c):
                continue
            if c[0] in ("segment", "arc", "via", "zone") or c[0].startswith("gr_"):
                yield c
            elif c[0] in ("footprint", "module"):
                for p in findall(c, "pad"):
                    yield p

    def select_nets(self, patterns: Sequence[str]) -> List[str]:
        names = self.net_names()
        sel = []
        for nm in names:
            for p in patterns:
                if nm == p or fnmatch.fnmatchcase(nm, p) or ("/" + nm) == p or nm == "/" + p:
                    sel.append(nm)
                    break
        return sel

    # -- layer helpers --------------------------------------------------------------
    def expand_layers(self, names) -> List[str]:
        out = []
        for n in names:
            n = str(n)
            if n in ("*.Cu",):
                out.extend(self.cu)
            elif n == "F&B.Cu":
                out.extend([self.cu[0], self.cu[-1]])
            elif n in self.cu:
                out.append(n)
        return [l for l in self.cu if l in set(out)]

    # -- extraction -----------------------------------------------------------------
    def extract(self, nets: Sequence[str]) -> Dict[str, NetGeom]:
        o = self.opts
        wanted = set(nets)
        geo: Dict[str, NetGeom] = {n: NetGeom(n) for n in nets}
        npth: List[Hole] = []
        pending_flash = []   # (net, layers_all, keep_end, end_layers, builder) for remove_unused

        for c in self.root[1:]:
            if not is_node(c):
                continue
            tag = c[0]
            if tag == "segment" and o.include_tracks:
                net = self.item_net(c)
                if net in wanted:
                    ly = str(val(c, "layer", ""))
                    if ly in self.cu:
                        w = fval(c, "width", 0.2)
                        geo[net].layers[ly].append(("seg", k2f(xy(c, "start")), k2f(xy(c, "end")), w))
                        geo[net].routed.add(ly)
                        geo[net].counts["segments"] += 1
            elif tag == "arc" and o.include_tracks:
                net = self.item_net(c)
                if net in wanted:
                    ly = str(val(c, "layer", ""))
                    if ly in self.cu:
                        w = fval(c, "width", 0.2)
                        geo[net].layers[ly].append(("arc", k2f(xy(c, "start")), k2f(xy(c, "mid")),
                                                    k2f(xy(c, "end")), w))
                        geo[net].routed.add(ly)
                        geo[net].counts["arcs"] += 1
            elif tag == "via" and o.include_vias:
                net = self.item_net(c)
                if net in wanted:
                    self._via(c, geo[net], pending_flash)
            elif tag == "zone" and o.include_zones:
                net = self.item_net(c)
                if net in wanted and not find(c, "keepout"):
                    self._zone(c, geo[net])
            elif tag.startswith("gr_") and o.include_graphics:
                net = self.item_net(c)
                if net in wanted:
                    ly = str(val(c, "layer", ""))
                    if ly in self.cu:
                        prims = self._graphic(c, (0.0, 0.0), 0.0)
                        geo[net].layers[ly].extend(prims)
                        geo[net].routed.add(ly)
                        geo[net].counts["graphics"] += len(prims)
            elif tag in ("footprint", "module"):
                self._footprint(c, geo, wanted, npth, pending_flash)

        # Resolve remove_unused_layers: flash pads only on layers where the net is routed
        # (tracks/zones/graphics). Approximation of KiCad's per-item connectivity test.
        for net, layers_all, keep_end, ends, prim in pending_flash:
            for ly in layers_all:
                if (keep_end and ly in ends) or ly in geo[net].routed:
                    geo[net].layers[ly].append(prim(ly))

        for g in geo.values():
            g.holes.extend(npth)   # NPTH: cut through any copper they hit
        self.npth = npth
        return geo

    def _padstack_override(self, node, layer):
        """KiCad 9 padstacks: return (shape, size, extras-node) override for `layer`."""
        ps = find(node, "padstack")
        if not ps:
            return None
        mode = str(val(ps, "mode", "normal"))
        if mode == "normal":
            return None
        cands = [layer]
        if layer not in (self.cu[0], self.cu[-1]):
            cands.append("Inner")
        for ln in findall(ps, "layer"):
            if str(ln[1]) in cands:
                return ln
        return None

    def _via(self, c, g: NetGeom, pending):
        o = self.opts
        at = k2f(xy(c, "at"))
        size = fval(c, "size", 0.6)
        drill = fval(c, "drill", 0.3)
        lys = [str(l) for l in (find(c, "layers") or ["layers", self.cu[0], self.cu[-1]])[1:]]
        lys = [l for l in lys if l in self.cu]
        if len(lys) < 2:
            lys = [self.cu[0], self.cu[-1]]
        span = self.stackup.span(lys[0], lys[1])
        top, bot = span[0].name, span[-1].name
        vtype = str(val(c, "type", "")).lower()
        micro = flag(c, "micro") or vtype == "micro"
        # KiCad writes "blind" for both blind and buried vias: classify by span
        outer = top == self.cu[0] or bot == self.cu[-1]
        if micro:
            kind = "micro"
        elif top == self.cu[0] and bot == self.cu[-1]:
            kind = "via"
        elif outer:
            kind = "blind"
        else:
            kind = "buried"
        solid = (micro and o.microvia_solid) or (o.filled_vias_solid and flag(c, "filling"))
        hole_obj = Hole(at, (drill, drill), 0.0, top, bot, True, solid, kind)
        g.holes.append(hole_obj)
        g.counts["vias_" + kind] += 1

        def pad_for(ly, size=size, at=at, c=c):
            ov = self._padstack_override(c, ly)
            s = fval(ov, "size", size) if ov else size
            return ("disk", at, s / 2.0)
        sides = {}
        if top == self.cu[0]:
            sides["top"] = top
        if bot == self.cu[-1]:
            sides["bottom"] = bot
        if sides:
            kx, ky = at[0], -at[1]
            g.terminals.append(TermCandidate(
                "via", "VIA(%.3f,%.3f)%s" % (kx, ky, "" if kind == "via" else "-" + kind),
                {sd: pad_for(ly) for sd, ly in sides.items()}, sides,
                "top" if "top" in sides else "bottom", at, hole_obj,
                "%s %s-%s d=%.3f drill=%.3f" % (kind, top, bot, size, drill)))
        remove_unused = flag(c, "remove_unused_layers")
        keep_end = flag(c, "keep_end_layers") or not find(c, "keep_end_layers")
        names = [l.name for l in span]
        if remove_unused:
            pending.append((g.name, names, keep_end, {top, bot}, pad_for))
        else:
            for ly in names:
                g.layers[ly].append(pad_for(ly))

    def _zone(self, c, g: NetGeom):
        filled = findall(c, "filled_polygon")
        if not filled:
            g.warnings.append("Zone on %s has no fill data (refill zones in KiCad and save)"
                              % (val(c, "layer") or val(c, "layers")))
            return
        for fp in filled:
            ly = str(val(fp, "layer", val(c, "layer", "")))
            if ly not in self.cu:
                continue
            pts = self._pts(find(fp, "pts"))
            for outer, holes in unfracture(pts):
                tol = self.opts.poly_tol
                outer = clean_loop(outer, tol)
                holes = [h for h in (clean_loop(h, tol) for h in holes) if h]
                if not outer:
                    continue
                g.layers[ly].append(("poly", outer, holes, 0.0))
                g.routed.add(ly)
                g.counts["zone_islands"] += 1

    def _pts(self, ptsnode, origin=(0.0, 0.0), angle=0.0) -> List[Pt]:
        out: List[Pt] = []
        if not ptsnode:
            return out
        for p in ptsnode[1:]:
            if not is_node(p):
                continue
            if p[0] == "xy":
                out.append(add(rot(k2f((float(p[1]), float(p[2]))), angle), origin))
            elif p[0] == "arc":
                a = add(rot(k2f(xy(p, "start")), angle), origin)
                m = add(rot(k2f(xy(p, "mid")), angle), origin)
                b = add(rot(k2f(xy(p, "end")), angle), origin)
                ap = arc_points(a, m, b, self.opts.arc_segments)
                if out and math.hypot(out[-1][0] - ap[0][0], out[-1][1] - ap[0][1]) < 1e-9:
                    ap = ap[1:]
                out.extend(ap)
        return out

    def _stroke_w(self, c, default=0.0):
        s = find(c, "stroke")
        if s is not None:
            w = fval(s, "width", None)
            if w is not None:
                return w
        return fval(c, "width", default)

    def _filled(self, c):
        f = val(c, "fill", None)
        return str(f).lower() in ("solid", "yes", "true")

    def _graphic(self, c, origin: Pt, angle: float) -> list:
        """Copper graphic shapes; coordinates local to (origin, angle) in FC frame."""
        T = lambda p: add(rot(k2f(p), angle), origin)
        tag = c[0].replace("fp_", "gr_")
        w = self._stroke_w(c, 0.0)
        prims = []
        if tag == "gr_line":
            prims.append(("seg", T(xy(c, "start")), T(xy(c, "end")), w))
        elif tag == "gr_arc":
            if find(c, "mid"):
                prims.append(("arc", T(xy(c, "start")), T(xy(c, "mid")), T(xy(c, "end")), w))
        elif tag == "gr_circle":
            ctr = T(xy(c, "center"))
            e = T(xy(c, "end"))
            r = math.hypot(e[0] - ctr[0], e[1] - ctr[1])
            if self._filled(c):
                prims.append(("disk", ctr, r + w / 2.0))
            elif w > 0:
                prims.append(("ring", ctr, r + w / 2.0, max(r - w / 2.0, 0.0)))
        elif tag == "gr_rect":
            s, e = xy(c, "start"), xy(c, "end")
            corners = [T((s[0], s[1])), T((e[0], s[1])), T((e[0], e[1])), T((s[0], e[1]))]
            if self._filled(c):
                prims.append(("poly", corners, [], w / 2.0))
            elif w > 0:
                for i in range(4):
                    prims.append(("seg", corners[i], corners[(i + 1) % 4], w))
        elif tag in ("gr_poly", "gr_curve"):
            pts = self._pts(find(c, "pts"), origin, angle)
            if tag == "gr_curve":
                pts = _bezier(pts, self.opts.arc_segments)
            if len(pts) >= 3 and self._filled(c):
                pts = clean_loop(pts, self.opts.poly_tol) or pts
                prims.append(("poly", pts, [], w / 2.0))
            elif w > 0:
                closed = tag == "gr_poly"
                n = len(pts)
                for i in range(n if closed else n - 1):
                    prims.append(("seg", pts[i], pts[(i + 1) % n], w))
        return prims

    def _footprint(self, fp, geo, wanted, npth, pending):
        o = self.opts
        fside = "bottom" if str(val(fp, "layer", "F.Cu")) == "B.Cu" else "top"
        seen: Dict[str, int] = defaultdict(int)
        fat = find(fp, "at")
        fpos = k2f((float(fat[1]), float(fat[2])))
        fang = float(fat[3]) if len(fat) > 3 else 0.0
        ref = ""
        for p in findall(fp, "property"):
            if len(p) > 2 and p[1] == "Reference":
                ref = str(p[2])
        for t in findall(fp, "fp_text"):
            if len(t) > 2 and t[1] == "reference":
                ref = str(t[2])

        for pad in findall(fp, "pad"):
            ptype = str(pad[2])
            shape = str(pad[3])
            pat = find(pad, "at")
            ploc = k2f((float(pat[1]), float(pat[2])))
            pang = float(pat[3]) if len(pat) > 3 else 0.0   # absolute orientation
            ppos = add(fpos, rot(ploc, fang))
            dr = find(pad, "drill")
            hole = None
            if dr is not None and ptype in ("thru_hole", "np_thru_hole"):
                nums = [float(x) for x in dr[1:] if isinstance(x, str) and _isnum(x)]
                if nums:
                    oval = "oval" in atoms(dr)
                    hw = nums[0]
                    hh = nums[1] if (oval and len(nums) > 1) else hw
                    hole = (hw, hh)
            off = (0.0, 0.0)
            if dr is not None and find(dr, "offset"):
                off = k2f(xy(dr, "offset"))
            ref_pad = "%s.%s" % (ref, pad[1])

            if ptype == "np_thru_hole":
                if hole:
                    npth.append(Hole(ppos, hole, pang, self.cu[0], self.cu[-1], False, False,
                                     "npth", ref_pad))
                continue
            net = self.item_net(pad)
            if net not in wanted or not o.include_pads:
                continue
            g = geo[net]
            players = self.expand_layers(find(pad, "layers")[1:] if find(pad, "layers") else [])
            if ptype == "thru_hole":
                players = list(self.cu)    # PTH always spans all copper

            def pad_prim(ly, pad=pad, shape=shape, ppos=ppos, pang=pang, off=off, fpos=fpos):
                ov = self._padstack_override(pad, ly)
                sz_node = ov if (ov is not None and find(ov, "size")) else pad
                s = find(sz_node, "size")
                size = (float(s[1]), float(s[2]))
                shp = str(val(ov, "shape", shape)) if ov is not None else shape
                src = ov if ov is not None else pad
                extras = {
                    "rratio": fval(src, "roundrect_rratio", fval(pad, "roundrect_rratio", 0.25)),
                    "cratio": fval(src, "chamfer_ratio", fval(pad, "chamfer_ratio", 0.0)),
                    "chamfer": [str(x) for x in (find(src, "chamfer") or find(pad, "chamfer") or ["c"])[1:]],
                    "delta": xy(src, "rect_delta") or xy(pad, "rect_delta") or (0.0, 0.0),
                }
                if shp == "custom":
                    extras["anchor"] = str(val(find(pad, "options") or ["o"], "anchor", "circle"))
                    extras["prims"] = []
                    prn = find(pad, "primitives")
                    if prn:
                        for pr in prn[1:]:
                            if is_node(pr):
                                extras["prims"].extend(self._graphic(pr, (0.0, 0.0), 0.0))
                center = add(ppos, rot(off, pang))
                return ("pad", shp, center, size, pang, extras)

            hole_obj = (Hole(ppos, hole, pang, self.cu[0], self.cu[-1], True, False, "pth", ref_pad)
                        if hole else None)
            sides = {sd: ly for sd, ly in (("top", self.cu[0]), ("bottom", self.cu[-1]))
                     if ly in players}
            if sides:
                seen[ref_pad] += 1
                lbl = ref_pad if seen[ref_pad] == 1 else "%s#%d" % (ref_pad, seen[ref_pad])
                g.terminals.append(TermCandidate(
                    "pad", lbl, {sd: pad_prim(ly) for sd, ly in sides.items()}, sides,
                    fside if fside in sides else next(iter(sides)),
                    add(ppos, rot(off, pang)), hole_obj,
                    "%s %s %.3fx%.3f%s" % (ptype, shape, float(find(pad, "size")[1]),
                                           float(find(pad, "size")[2]),
                                           " drill %.3f" % hole[0] if hole else "")))
            if hole:
                g.holes.append(hole_obj)
                g.counts["pads_tht"] += 1
                remove_unused = flag(pad, "remove_unused_layers")
                keep_end = flag(pad, "keep_end_layers") or not find(pad, "keep_end_layers")
                if remove_unused:
                    pending.append((net, players, keep_end, {self.cu[0], self.cu[-1]}, pad_prim))
                    continue
            else:
                g.counts["pads_smd"] += 1
            for ly in players:
                g.layers[ly].append(pad_prim(ly))


def _isnum(s) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _bezier(pts, n):
    if len(pts) != 4:
        return pts
    out = []
    for i in range(n + 1):
        t = i / n
        a = (1 - t) ** 3
        b = 3 * (1 - t) ** 2 * t
        c = 3 * (1 - t) * t * t
        d = t ** 3
        out.append((a * pts[0][0] + b * pts[1][0] + c * pts[2][0] + d * pts[3][0],
                    a * pts[0][1] + b * pts[1][1] + c * pts[2][1] + d * pts[3][1]))
    return out


