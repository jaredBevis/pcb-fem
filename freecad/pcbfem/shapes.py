# SPDX-License-Identifier: LGPL-2.1-or-later
"""FreeCAD shape construction: 2D copper primitives, pads, barrels, net solids."""
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

from .kicad import (Options, Stackup, NetGeom, circumcenter, arc_points, k2f)

# ----------------------------------------------------------------------------------
# FreeCAD back end
# ----------------------------------------------------------------------------------


def _fc():
    import FreeCAD as App   # noqa
    import Part             # noqa
    for mod in ("PartDesign", "_PartDesign"):   # registers PartDesign::Body headless
        try:
            __import__(mod)
            break
        except ImportError:
            pass
    return App, Part


class ShapeBuilder:
    def __init__(self, opts: Options):
        self.App, self.Part = _fc()
        self.o = opts
        self.V = self.App.Vector

    # basic 2D faces at z=0 -----------------------------------------------------------
    def v(self, p):
        return self.V(p[0], p[1], 0)

    def disk(self, c, r):
        P = self.Part
        return P.Face(P.Wire(P.makeCircle(r, self.v(c))))

    def stadium(self, p0, p1, w):
        P, v = self.Part, self.v
        h = w / 2.0
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        L = math.hypot(dx, dy)
        if L < self.o.min_feature:
            return self.disk(p0, h)
        d = (dx / L, dy / L)
        n = (-d[1], d[0])
        a1 = (p0[0] + n[0] * h, p0[1] + n[1] * h)
        a2 = (p1[0] + n[0] * h, p1[1] + n[1] * h)
        a3 = (p1[0] - n[0] * h, p1[1] - n[1] * h)
        a4 = (p0[0] - n[0] * h, p0[1] - n[1] * h)
        e = [P.LineSegment(v(a1), v(a2)).toShape(),
             P.Arc(v(a2), v((p1[0] + d[0] * h, p1[1] + d[1] * h)), v(a3)).toShape(),
             P.LineSegment(v(a3), v(a4)).toShape(),
             P.Arc(v(a4), v((p0[0] - d[0] * h, p0[1] - d[1] * h)), v(a1)).toShape()]
        return P.Face(P.Wire(e))

    def arc_track(self, s, m, e, w):
        P, v = self.Part, self.v
        c = circumcenter(s, m, e)
        if c is None:
            return [self.stadium(s, e, w)]
        R = math.hypot(s[0] - c[0], s[1] - c[1])
        h = w / 2.0
        if R - h <= self.o.min_feature:
            pts = arc_points(s, m, e, 32)
            return [self.stadium(pts[i], pts[i + 1], w) for i in range(len(pts) - 1)]

        def rad(p, r):
            a = math.atan2(p[1] - c[1], p[0] - c[0])
            return (c[0] + r * math.cos(a), c[1] + r * math.sin(a))
        so, mo, eo = rad(s, R + h), rad(m, R + h), rad(e, R + h)
        si, mi, ei = rad(s, R - h), rad(m, R - h), rad(e, R - h)
        edges = [P.Arc(v(so), v(mo), v(eo)).toShape(),
                 P.LineSegment(v(eo), v(ei)).toShape(),
                 P.Arc(v(ei), v(mi), v(si)).toShape(),
                 P.LineSegment(v(si), v(so)).toShape()]
        return [P.Face(P.Wire(edges)), self.disk(s, h), self.disk(e, h)]

    def polygon(self, outer, holes=(), inflate=0.0):
        P, v = self.Part, self.v

        def wire(loop):
            pts = [v(p) for p in loop]
            if (pts[0] - pts[-1]).Length > 1e-9:
                pts.append(pts[0])
            return P.makePolygon(pts)
        wires = [wire(outer)] + [wire(h) for h in holes if len(h) >= 3]
        try:
            f = P.makeFace(wires, "Part::FaceMakerBullseye")
        except Exception:
            f = P.Face(wires[0])
            for hw in wires[1:]:
                f = f.cut(P.Face(hw))
        if inflate > 0:
            f = f.makeOffset2D(inflate, 0, False, False, False)
        return f

    def corner_rect(self, w, h, corners):
        """Rectangle centred at origin. corners: dict BR/TR/TL/BL -> (type, k)."""
        P, v = self.Part, self.v  # noqa
        order = [("BR", (1, -1), (1, 0), (0, 1)), ("TR", (1, 1), (0, 1), (-1, 0)),
                 ("TL", (-1, 1), (-1, 0), (0, -1)), ("BL", (-1, -1), (0, -1), (1, 0))]
        lim = min(w, h) / 2.0
        segs = []   # list of (entry, exit, kind, center)
        for nm, (sx, sy), din, dout in order:
            C = (sx * w / 2.0, sy * h / 2.0)
            typ, k = corners.get(nm, ("none", 0.0))
            k = min(max(k, 0.0), lim * 0.999999)
            if typ == "none" or k <= 1e-9:
                segs.append((C, C, "none", None))
                continue
            en = (C[0] - k * din[0], C[1] - k * din[1])
            ex = (C[0] + k * dout[0], C[1] + k * dout[1])
            if typ == "round":
                cc = (C[0] + k * (dout[0] - din[0]), C[1] + k * (dout[1] - din[1]))
                s2 = k / math.sqrt(2)
                mid = (cc[0] + s2 * (din[0] - dout[0]), cc[1] + s2 * (din[1] - dout[1]))
                segs.append((en, ex, "round", mid))
            else:
                segs.append((en, ex, "chamfer", None))
        edges = []
        for i, (en, ex, kind, mid) in enumerate(segs):
            if kind == "round":
                edges.append(P.Arc(v(en), v(mid), v(ex)).toShape())
            elif kind == "chamfer":
                edges.append(P.LineSegment(v(en), v(ex)).toShape())
            nxt = segs[(i + 1) % 4][0]
            if math.hypot(nxt[0] - ex[0], nxt[1] - ex[1]) > 1e-9:
                edges.append(P.LineSegment(v(ex), v(nxt)).toShape())
        return P.Face(P.Wire(edges))

    def pad(self, shape, center, size, angle, ex):
        w, h = size
        if shape == "circle":
            f = self.disk((0, 0), w / 2.0)
        elif shape == "oval":
            if abs(w - h) < 1e-9:
                f = self.disk((0, 0), w / 2.0)
            elif w > h:
                f = self.stadium((-(w - h) / 2, 0), ((w - h) / 2, 0), h)
            else:
                f = self.stadium((0, -(h - w) / 2), (0, (h - w) / 2), w)
        elif shape in ("rect", "roundrect", "chamfered_rect"):
            corners = {}
            if shape in ("roundrect", "chamfered_rect"):
                r = ex["rratio"] * min(w, h) if shape == "roundrect" or ex["rratio"] else 0.0
                for c in ("BR", "TR", "TL", "BL"):
                    corners[c] = ("round", r)
            if shape == "chamfered_rect" and ex["cratio"] > 0:
                k = ex["cratio"] * min(w, h)
                cmap = {"top_left": "TL", "top_right": "TR", "bottom_left": "BL",
                        "bottom_right": "BR"}
                for c in ex["chamfer"]:
                    if c in cmap:
                        corners[cmap[c]] = ("chamfer", k)
            f = self.corner_rect(w, h, corners)
        elif shape == "trapezoid":
            dx, dy = ex["delta"][0] / 2.0, ex["delta"][1] / 2.0
            hx, hy = w / 2.0, h / 2.0
            kc = [(-hx - dy, hy + dx), (hx + dy, hy - dx), (hx - dy, -hy + dx), (-hx + dy, -hy - dx)]
            f = self.polygon([k2f(p) for p in kc])
        elif shape == "custom":
            faces = []
            if ex.get("anchor") == "rect":
                faces.append(self.corner_rect(w, h, {}))
            else:
                faces.append(self.disk((0, 0), w / 2.0))
            faces.extend(self.prims2d(ex.get("prims", [])))
            f = self.fuse2d(faces)
        else:
            f = self.disk((0, 0), w / 2.0)
        f = f.copy()
        if angle:
            f.rotate(self.V(0, 0, 0), self.V(0, 0, 1), angle)
        f.translate(self.v(center))
        return f

    def slot(self, center, size, angle, shrink=0.0):
        w, h = size[0] - 2 * shrink, size[1] - 2 * shrink
        if w <= self.o.min_feature or h <= self.o.min_feature:
            return None
        if abs(w - h) < 1e-9:
            return self.disk(center, w / 2.0)
        if w > h:
            f = self.stadium((-(w - h) / 2, 0), ((w - h) / 2, 0), h)
        else:
            f = self.stadium((0, -(h - w) / 2), (0, (h - w) / 2), w)
        f = f.copy()
        if angle:
            f.rotate(self.V(0, 0, 0), self.V(0, 0, 1), angle)
        f.translate(self.v(center))
        return f

    def prims2d(self, prims) -> list:
        faces = []
        for p in prims:
            k = p[0]
            try:
                if k == "seg":
                    if p[3] > self.o.min_feature:
                        faces.append(self.stadium(p[1], p[2], p[3]))
                elif k == "arc":
                    if p[4] > self.o.min_feature:
                        faces.extend(self.arc_track(p[1], p[2], p[3], p[4]))
                elif k == "disk":
                    faces.append(self.disk(p[1], p[2]))
                elif k == "ring":
                    f = self.disk(p[1], p[2])
                    if p[3] > self.o.min_feature:
                        f = f.cut(self.disk(p[1], p[3]))
                    faces.append(f)
                elif k == "poly":
                    if len(p[1]) >= 3:
                        faces.append(self.polygon(p[1], p[2], p[3]))
                elif k == "pad":
                    faces.append(self.pad(*p[1:]))
            except Exception as e:   # keep going; report
                self.App.Console.PrintWarning("kicad2fc: skipped primitive %s: %s\n" % (k, e))
        return faces

    def up(self, shape):
        """Make every face point +Z (mixed orientations break the boolean fuse)."""
        out = []
        for f in shape.Faces:
            try:
                u0, u1, v0, v1 = f.ParameterRange
                if f.normalAt((u0 + u1) / 2, (v0 + v1) / 2).z < 0:
                    f = f.copy()
                    f.reverse()
            except Exception:
                pass
            out.append(f)
        return out[0] if len(out) == 1 else self.Part.Compound(out)

    def fuse2d(self, faces):
        faces = [self.up(f) for f in faces if f is not None and not f.isNull() and f.Area > 1e-12]
        if not faces:
            return None
        if len(faces) == 1:
            r = faces[0]
        else:
            r = faces[0].multiFuse(faces[1:])
        if self.o.refine:
            try:
                r = r.removeSplitter()
            except Exception:
                pass
        return r

    # 3D --------------------------------------------------------------------------------
    def build_net(self, g: NetGeom, st: Stackup):
        V = self.V
        solids = []
        report = {"net": g.name, "layers": {}, "barrels": 0}
        for lname, prims in g.layers.items():
            if not prims:
                continue
            L = st.by_name[lname]
            face = self.fuse2d(self.prims2d(prims))
            if face is None:
                continue
            # cut holes through this layer
            cuts = []
            bb = face.BoundBox
            for hl in g.holes:
                if lname not in [l.name for l in st.span(hl.top, hl.bot)]:
                    continue
                if hl.solid:
                    continue
                shrink = self.o.plating if hl.plated else 0.0
                f = self.slot(hl.center, hl.size, hl.angle, shrink)
                if f is None:
                    continue
                fb = f.BoundBox
                if fb.XMax < bb.XMin or fb.XMin > bb.XMax or fb.YMax < bb.YMin or fb.YMin > bb.YMax:
                    continue
                cuts.append(f)
            if cuts:
                face = face.cut(cuts[0].multiFuse(cuts[1:]) if len(cuts) > 1 else cuts[0])
            for f in face.Faces:
                s = f.extrude(V(0, 0, L.thickness))
                s.translate(V(0, 0, L.z_bot))
                solids.append(s)
            report["layers"][lname] = round(face.Area, 6)

        for hl in g.holes:
            if not hl.plated:
                continue
            span = st.span(hl.top, hl.bot)
            z0, z1 = span[-1].z_bot, span[0].z_top
            outer = self.slot(hl.center, hl.size, hl.angle, 0.0)
            if outer is None:
                continue
            if not hl.solid:
                inner = self.slot(hl.center, hl.size, hl.angle, self.o.plating)
                if inner is not None:
                    outer = outer.cut(inner)
            for f in outer.Faces:
                b = f.extrude(V(0, 0, z1 - z0))
                b.translate(V(0, 0, z0))
                solids.append(b)
            report["barrels"] += 1

        if not solids:
            return None, report
        shape = solids[0].multiFuse(solids[1:]) if len(solids) > 1 else solids[0]
        if self.o.refine:
            try:
                shape = shape.removeSplitter()
            except Exception:
                pass
        report["solids"] = len(shape.Solids)
        report["volume_mm3"] = round(shape.Volume, 6)
        report["valid"] = bool(shape.isValid())
        return shape, report


def _safe_name(net: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", net.strip("/")) or "net"
    if s[0].isdigit():
        s = "N_" + s
    return s


_PALETTE = [(0.85, 0.45, 0.10), (0.20, 0.55, 0.85), (0.30, 0.70, 0.30), (0.80, 0.25, 0.30),
            (0.60, 0.40, 0.80), (0.95, 0.75, 0.20), (0.25, 0.75, 0.75), (0.70, 0.55, 0.40)]


