# SPDX-License-Identifier: LGPL-2.1-or-later
"""FEM electrical setup: terminals on pads/vias, Elmer static-current analysis, results."""
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

from .kicad import Board, Options, TermCandidate, Hole
from .shapes import ShapeBuilder, _fc, _safe_name

# ----------------------------------------------------------------------------------
# FEM electrical setup: terminals on pads / vias
# ----------------------------------------------------------------------------------
#
# A *terminal* is a named group of pads/vias of one net that acts as one electrical
# connection of the real circuit (a component lead, connector pin, probe ...).
# Each pad/via in a terminal gets a small copper "stub": the pad outline scaled by
# `Scale` (default 0.8 = 80 % linear) extruded `StubHeight` outward from the outer
# copper surface. The flat end face of every stub is a clean boundary-condition face
# that sits only on that pad - no edge picking, no overlap with traces or planes.
# For drilled pads/vias, `FillHoles` also fills the barrel with copper (soldered
# lead) so the stub always connects even when 80 % of the pad is smaller than the hole.
#
# Terminal types
#   Current : a total current I [A] is pushed INTO the net through the terminal.
#             Elmer's Static Current equation takes a normal current density, so
#             J = I / sum(A_i)            ("Uniform J": current shared by area), or
#             J_i = (I / N) / A_i         ("Equal per item": same current per pad/via).
#             Use a negative value to draw current out of the net.
#   Ground  : potential 0 V (every model needs at least one potential terminal)
#   Voltage : fixed potential V
#
# Terminals are stored as Part::Feature objects ("PcbTerm_<name>") holding the stub
# compound plus their settings as properties, so they survive save/reload and can be
# edited in the property editor. build_fem_model() fuses net copper + stubs into
# "<net>_FEM", creates/updates an Elmer analysis and (re)creates the constraints with
# the computed current densities, re-finding the stub end faces geometrically.

TERM_TYPES = ["Current", "Ground", "Voltage"]
TERM_DIST = ["Uniform J", "Equal per item"]
COPPER = {"Name": "Copper-Generic", "CardName": "Copper-Generic",
          "ElectricalConductivity": "59.6 MS/m", "Density": "8960 kg/m^3",
          "ThermalConductivity": "401 W/m/K", "SpecificHeat": "385 J/kg/K",
          "ThermalExpansionCoefficient": "16.5 um/m/K",
          "Description": "Annealed copper at 20 C (added by kicad_pcb_to_freecad)"}

_BOARD_CACHE: Dict[tuple, tuple] = {}


def net_objects(doc) -> list:
    """Objects created by the converter (Bodies or Part features carrying NetName)."""
    return [o for o in doc.Objects
            if hasattr(o, "NetName") and hasattr(o, "SourceBoard")
            and not hasattr(o, "TermType") and not o.Name.endswith("_FEM")]


def _opts_from(obj) -> Options:
    o = Options()
    js = getattr(obj, "PcbOptions", "")
    if js:
        try:
            d = json.loads(js)
            for k, v in d.items():
                if hasattr(o, k):
                    setattr(o, k, v)
        except Exception:
            pass
    return o


def net_candidates(net_obj) -> Tuple["Board", List[TermCandidate]]:
    """Re-parse the source board and return (board, terminal candidates) for a net object."""
    path = net_obj.SourceBoard
    if not os.path.isfile(path):
        raise FileNotFoundError("Source board not found: %s (set the SourceBoard property)" % path)
    opts = _opts_from(net_obj)
    key = (path, os.path.getmtime(path), net_obj.NetName, json.dumps(asdict(opts), sort_keys=True))
    if key not in _BOARD_CACHE:
        b = Board(path, opts)
        g = b.extract([net_obj.NetName])[net_obj.NetName]
        _BOARD_CACHE[key] = (b, g.terminals)
    return _BOARD_CACHE[key]


def match_items(cands: List[TermCandidate], patterns: Sequence[str]) -> List[Tuple[TermCandidate, str]]:
    """
    Resolve item patterns to (candidate, side). Pattern syntax: <label-wildcard>[@top|@bottom]
    e.g. "U1.1", "U1.*", "J1.2@bottom", "VIA(114.000,116.000)*", "VIA*@top".
    Without @side a candidate's default side is used (footprint side for pads).
    """
    out, seen = [], set()
    for pat in patterns:
        pat = pat.strip()
        if not pat:
            continue
        side = None
        if "@" in pat:
            pat, side = pat.rsplit("@", 1)
            side = side.lower()
        hit = False
        for c in cands:
            if c.label == pat or fnmatch.fnmatchcase(c.label, pat):
                sd = side or c.default_side
                if sd not in c.sides:
                    continue
                if c.key(sd) not in seen:
                    seen.add(c.key(sd))
                    out.append((c, sd))
                hit = True
        if not hit:
            raise ValueError("No pad/via matches '%s%s'" % (pat, "@" + side if side else ""))
    return out


class TerminalBuilder:
    """Builds stub solids for terminal items (FreeCAD back end)."""

    def __init__(self, board: "Board", opts: Options):
        self.b = board
        self.sb = ShapeBuilder(opts)
        self.V = self.sb.V

    def pad_face(self, cand: TermCandidate, side: str):
        f = self.sb.prims2d([cand.sides[side]])
        return self.sb.up(f[0]) if f else None

    def stub(self, cand: TermCandidate, side: str, scale=0.8, height=0.05, fill=True):
        """Return (list_of_solids, end_face_info) for one item."""
        V = self.V
        st = self.b.stackup
        prim = cand.sides[side]
        ctr = prim[2] if prim[0] == "pad" else prim[1]
        f = self.pad_face(cand, side)
        if f is None:
            raise ValueError("Could not build pad shape for %s" % cand.label)
        f = f.copy()
        f.scale(scale, V(ctr[0], ctr[1], 0))
        L = st.by_name[cand.layers[side]]
        if side == "top":
            z0, dz = L.z_top, height
        else:
            z0, dz = L.z_bot, -height
        s = f.extrude(V(0, 0, dz))
        s.translate(V(0, 0, z0))
        solids = [s]
        hl = cand.hole
        if fill and hl is not None and hl.plated and not hl.solid:
            span = st.span(hl.top, hl.bot)
            zb, zt = span[-1].z_bot, span[0].z_top
            # overlap half the wall so the plug fuses robustly with the barrel
            plug = self.sb.slot(hl.center, hl.size, hl.angle, self.sb.o.plating / 2.0)
            if plug is not None:
                p = plug.extrude(V(0, 0, zt - zb))
                p.translate(V(0, 0, zb))
                solids.append(p)
        com = f.CenterOfMass
        info = {"item": cand.key(side), "z": z0 + dz, "x": com.x, "y": com.y,
                "area": f.Area, "nz": 1.0 if dz > 0 else -1.0}
        return solids, info


def _add_prop(obj, typ, name, group, doc, value=None):
    if not hasattr(obj, name):
        obj.addProperty(typ, name, group, doc)
    if value is not None:
        setattr(obj, name, value)


def terminals_for(doc, net_name: str) -> list:
    return [o for o in doc.Objects if hasattr(o, "TermType") and getattr(o, "PcbNet", None) == net_name]


def compute_stubs(obj):
    """Build the stub compound for a terminal object from its properties.
    Returns (shape, stub_infos, resolved_item_keys); raises ValueError on problems."""
    Part = _fc()[1]
    if obj.NetObject is None:
        raise ValueError("terminal has no NetObject")
    board, cands = net_candidates(obj.NetObject)
    resolved = match_items(cands, list(obj.Items))
    if not resolved:
        raise ValueError("no pads/vias")
    tb = TerminalBuilder(board, _opts_from(obj.NetObject))
    solids, infos = [], []
    for c, sd in resolved:
        s, info = tb.stub(c, sd, obj.Scale, obj.StubHeight, obj.FillHoles)
        solids.extend(s)
        infos.append(info)
    return Part.makeCompound(solids), infos, [c.key(sd) for c, sd in resolved]


def create_terminal(doc, net_obj, name: str, items: Sequence[str], ttype="Current", value=1.0,
                    distribution="Uniform J", scale=None, height=None, fill_holes=None, obj=None):
    """
    Create (or update `obj`) a PCB FEM terminal object for `net_obj`.
    items: patterns understood by match_items(); value: amps (Current) or volts (Voltage).
    scale/height/fill_holes default to the user preferences.
    """
    from . import prefs
    from .objects import make_terminal, is_legacy_terminal
    if ttype not in TERM_TYPES:
        raise ValueError("Terminal type must be one of %s" % TERM_TYPES)
    scale = prefs.get("ContactScale") if scale is None else scale
    height = prefs.get("StubHeight") if height is None else height
    fill_holes = prefs.get("FillHoles") if fill_holes is None else fill_holes
    # validate before touching the document
    board, cands = net_candidates(net_obj)
    resolved = match_items(cands, items)
    if not resolved:
        raise ValueError("Terminal '%s' has no pads/vias" % name)
    if obj is not None and is_legacy_terminal(obj):
        doc.removeObject(obj.Name)
        obj = None
    if obj is None:
        obj = make_terminal(doc, name)
    obj.Label = name
    obj.NetObject = net_obj
    obj.PcbNet = net_obj.NetName
    obj.TermType = ttype
    if ttype == "Current":
        obj.Current_A = float(value)
    elif ttype == "Voltage":
        obj.Potential_V = float(value)
    else:
        obj.Potential_V = 0.0
    obj.Distribution = distribution
    obj.Items = [c.key(sd) for c, sd in resolved]
    obj.Scale = float(scale)
    obj.StubHeight = float(height)
    obj.FillHoles = bool(fill_holes)
    obj.Proxy.rebuild(obj)
    obj.purgeTouched()
    return obj


def rebuild_terminal(t):
    """Regenerate a terminal's stubs from its properties."""
    from .objects import is_legacy_terminal, upgrade_legacy_terminals
    if is_legacy_terminal(t):
        upgrade_legacy_terminals(t.Document)
        return None
    t.Proxy.rebuild(t)
    t.purgeTouched()
    return t


def _find_stub_face(shape, info, tol=1e-4):
    best, bestd = None, None
    for i, f in enumerate(shape.Faces):
        if f.Surface.__class__.__name__ != "Plane":
            continue
        bb = f.BoundBox
        if abs(bb.ZMin - info["z"]) > tol or abs(bb.ZMax - info["z"]) > tol:
            continue
        c = f.CenterOfMass
        d = math.hypot(c.x - info["x"], c.y - info["y"])
        if abs(f.Area - info["area"]) > 0.02 * info["area"] + 1e-9:
            continue
        if bestd is None or d < bestd:
            best, bestd = i, d
    if best is None or bestd > 0.05:
        return None
    return "Face%d" % (best + 1)


def _make_cd(doc, name):
    import ObjectsFem
    return ObjectsFem.makeConstraintCurrentDensity(doc, name)


def _make_pot(doc, name):
    import ObjectsFem
    fn = getattr(ObjectsFem, "makeConstraintElectromagnetic", None) or \
        getattr(ObjectsFem, "makeConstraintElectrostaticPotential")
    return fn(doc, name)


def _set_cd(obj, j_a_per_m2):
    App = _fc()[0]
    if hasattr(obj, "Mode"):
        obj.Mode = "Normal"
    obj.NormalCurrentDensity_re = App.Units.Quantity("%.12g A/m^2" % j_a_per_m2)
    if hasattr(obj, "NormalCurrentDensity_im"):
        obj.NormalCurrentDensity_im = App.Units.Quantity("0 A/m^2")


def _set_pot(obj, volts):
    App = _fc()[0]
    if hasattr(obj, "BoundaryCondition"):
        obj.BoundaryCondition = "Dirichlet"
    if hasattr(obj, "PotentialEnabled"):
        obj.PotentialEnabled = True
    obj.Potential = App.Units.Quantity("%.12g V" % volts)


def _assign_copper(mat):
    """Use FreeCAD's library card 'Copper-Generic' (Standard/Metal/Copper) so the material
    editor shows it; fall back to our own property set if the library is unavailable."""
    try:
        import Materials
        mm = Materials.MaterialManager()
        best = None
        for uuid in mm.Materials:
            m = mm.getMaterial(uuid)
            if m.Name != "Copper-Generic":
                continue
            props = dict(getattr(m, "Properties", {}) or {})
            if "ElectricalConductivity" in props:
                best = (uuid, m.Name, props)
                break
        if best:
            uuid, name, props = best
            props["Name"] = name
            props["CardName"] = name
            mat.Material = props
            if hasattr(mat, "UUID"):
                mat.UUID = uuid
            return
    except Exception:
        pass
    m = dict(mat.Material)
    m.update(COPPER)
    mat.Material = m


def configure_linear_solver(eq, kind="direct"):
    """
    FreeCAD's Elmer default (BiCGStab + ILU0, 500 its) does NOT converge on thin multilayer
    PCB copper, and FreeCAD writes 'Linear System Abort Not Converged = False', so Elmer then
    silently returns a wrong field. Use a direct solve (UMFPACK, shipped with Elmer) by default;
    'iterative' = CG + ILU2 with a high iteration cap for models too large for a direct solve
    (check the solver log for 'did not converge').
    """
    def setp(n, v):
        if hasattr(eq, n):
            try:
                setattr(eq, n, v)
            except Exception:
                pass
    if kind == "iterative":
        setp("LinearSolverType", "Iterative")
        setp("LinearIterativeMethod", "CG")
        setp("LinearPreconditioning", "ILU2")
        setp("LinearIterations", 20000)
        setp("LinearTolerance", 1e-10)
    else:
        setp("LinearSolverType", "Direct")
        setp("LinearDirectMethod", "Umfpack")
    setp("NonlinearIterations", 1)       # the static-current problem is linear


def build_fem_model(doc, net_obj, mesh_max=None, mesh_min=None, curvature=None,
                    joule_heating=None, mesh_now=None, log=print, linear_solver=None,
                    algorithm3d=None, drop_islands=None) -> dict:
    """
    Fuse net copper + all its terminal stubs into <net>_FEM, create/update an Elmer
    Static Current analysis and (re)create boundary conditions for every terminal.
    Arguments left as None take the user preferences (Edit > Preferences > PCB FEM).
    Returns a report dict (also logged).
    """
    from . import prefs
    from .objects import upgrade_legacy_terminals
    P = prefs.get
    mesh_max = P("MeshMax") if mesh_max is None else mesh_max
    mesh_min = P("MeshMin") if mesh_min is None else mesh_min
    curvature = P("Curvature") if curvature is None else curvature
    joule_heating = P("JouleHeating") if joule_heating is None else joule_heating
    mesh_now = P("MeshAfterBuild") if mesh_now is None else mesh_now
    linear_solver = P("LinearSolver") if linear_solver is None else linear_solver
    algorithm3d = P("Algorithm3D") if algorithm3d is None else algorithm3d
    drop_islands = P("DropIslands") if drop_islands is None else drop_islands
    App, Part = _fc()
    import ObjectsFem
    net = net_obj.NetName
    nm = _safe_name(net)
    upgrade_legacy_terminals(doc, log)
    terms = terminals_for(doc, net)
    if not terms:
        raise ValueError("Net %s has no terminals yet" % net)
    if not any(t.TermType in ("Ground", "Voltage") for t in terms):
        raise ValueError("Add at least one Ground or Voltage terminal: with current terminals "
                         "only, the potential is undefined and Elmer cannot solve.")

    # 1. geometry -------------------------------------------------------------------
    for t in terms:                       # keep stubs in sync with the board/properties
        rebuild_terminal(t)
    base = net_obj.Shape.copy()
    stubs = [s for t in terms for s in t.Shape.Solids]
    fused = base.multiFuse(stubs)
    try:
        fused = fused.removeSplitter()
    except Exception:
        pass
    if drop_islands and len(fused.Solids) > 1:
        # keep only copper islands touched by a terminal: the rest carries no current
        keep = set()
        for t in terms:
            for inf in json.loads(t.StubFaces):
                for si, sol in enumerate(fused.Solids):
                    if si not in keep and _find_stub_face(sol, inf) is not None:
                        keep.add(si)
                        break
        if keep and len(keep) < len(fused.Solids):
            sols = [fused.Solids[i] for i in sorted(keep)]
            log("  dropped %d copper island(s) without terminals (kept %d)"
                % (len(fused.Solids) - len(sols), len(sols)))
            fused = sols[0] if len(sols) == 1 else Part.makeCompound(sols)
    fem = doc.getObject(nm + "_FEM")
    if fem is None:
        fem = doc.addObject("Part::Feature", nm + "_FEM")
        fem.Label = net + " FEM"
        _add_prop(fem, "App::PropertyString", "PcbNet", "PCB", "KiCad net name", net)
    fem.Shape = fused

    # 2. analysis container -----------------------------------------------------------
    an = None
    for o in doc.Objects:
        if o.TypeId == "Fem::FemAnalysis" and getattr(o, "PcbNet", None) == net:
            an = o
    if an is None:
        an = ObjectsFem.makeAnalysis(doc, "Analysis_" + nm)
        an.Label = "Analysis " + net
        _add_prop(an, "App::PropertyString", "PcbNet", "PCB", "KiCad net name", net)
    members = list(an.Group)

    def find_member(pred):
        for m in members:
            if pred(m):
                return m
        return None

    solver = find_member(lambda m: getattr(getattr(m, "Proxy", None), "Type", "") == "Fem::SolverElmer"
                         or m.Name.startswith("SolverElmer"))
    if solver is None:
        solver = ObjectsFem.makeSolverElmer(doc, "SolverElmer_" + nm)
        an.addObject(solver)
        solver.SimulationType = "Steady State"
    eq = None
    for e in getattr(solver, "Group", []):
        if "StaticCurrent" in e.Name or "Static current" in e.Label:
            eq = e
    if eq is None:
        eq = ObjectsFem.makeEquationStaticCurrent(doc, solver)
    for p, v in (("CalculateJouleHeating", joule_heating), ("CalculateVolumeCurrent", True)):
        if hasattr(eq, p):
            setattr(eq, p, v)
    configure_linear_solver(eq, linear_solver)

    mat = find_member(lambda m: m.TypeId.startswith("App::MaterialObject"))
    if mat is None:
        mat = ObjectsFem.makeMaterialSolid(doc, "Copper_" + nm)
        an.addObject(mat)
    md = dict(mat.Material)
    ours_old = (not getattr(mat, "UUID", "")) and "kicad_pcb_to_freecad" in md.get("Description", "")
    if "ElectricalConductivity" not in md or ours_old:      # keep a user-chosen card
        _assign_copper(mat)

    mesh = find_member(lambda m: m.TypeId == "Fem::FemMeshShapeBaseObjectPython")
    if mesh is None:
        mesh = ObjectsFem.makeMeshGmsh(doc, "Mesh_" + nm)
        an.addObject(mesh)
        mesh.CharacteristicLengthMax = "%g mm" % mesh_max
        mesh.CharacteristicLengthMin = "%g mm" % mesh_min
        if hasattr(mesh, "MeshSizeFromCurvature"):
            mesh.MeshSizeFromCurvature = int(curvature)
        if hasattr(mesh, "ElementOrder"):
            mesh.ElementOrder = "2nd"
    mesh.Shape = fem
    # Explicit references: material + equation act on every solid of <net>_FEM.
    # (Empty lists also mean "all bodies" to the Elmer writer, but look unassigned in the GUI.)
    solid_refs = [(fem, tuple("Solid%d" % (i + 1) for i in range(len(fused.Solids))))]
    for o in (mat, eq):
        refs = list(getattr(o, "References", []) or [])
        if not refs or all(r[0] == fem or r[0] is None for r in refs):
            try:
                o.References = solid_refs
            except Exception as e:
                log("  note: could not set references on %s (%s)" % (o.Label, e))
    # applied on every build (also to meshes created earlier): HXT = multithreaded 3D mesher
    if algorithm3d and hasattr(mesh, "Algorithm3D"):
        try:
            mesh.Algorithm3D = algorithm3d
        except Exception as e:
            log("  note: could not set Algorithm3D=%s (%s)" % (algorithm3d, e))
    if algorithm3d == "HXT" and hasattr(mesh, "OptimizeNetgen"):
        mesh.OptimizeNetgen = True       # removes the flat sliver tets HXT can leave in thin copper
    if hasattr(mesh, "ParallelProcessing"):
        mesh.ParallelProcessing = True

    # 3. boundary conditions ------------------------------------------------------------
    for mbr in list(an.Group):
        if getattr(mbr, "PcbGenerated", False):
            doc.removeObject(mbr.Name)
    report = {"net": net, "fem_object": fem.Name, "analysis": an.Name, "solids": len(fused.Solids),
              "terminals": [], "warnings": []}
    face_solid = {}
    for si, sol in enumerate(fused.Solids):
        for f in sol.Faces:
            face_solid[(round(f.CenterOfMass.x, 5), round(f.CenterOfMass.y, 5),
                        round(f.CenterOfMass.z, 5))] = si
    solids_with_pot, solids_with_cur = set(), set()
    total_i = 0.0
    for t in terms:
        infos = json.loads(t.StubFaces)
        faces, areas = [], []
        for inf in infos:
            fn = _find_stub_face(fused, inf)
            if fn is None:
                report["warnings"].append("%s: stub face for %s not found (overlapping stubs?)"
                                          % (t.Label, inf["item"]))
                continue
            faces.append(fn)
            areas.append(fused.getElement(fn).Area)
            c = fused.getElement(fn).CenterOfMass
            si = face_solid.get((round(c.x, 5), round(c.y, 5), round(c.z, 5)))
            (solids_with_pot if t.TermType != "Current" else solids_with_cur).add(si)
        if not faces:
            continue
        tr = {"name": t.Label, "type": t.TermType, "items": list(t.Items), "faces": faces,
              "area_mm2": round(sum(areas), 6), "constraints": []}
        bc_name = "BC_" + _safe_name(t.Label)
        if t.TermType == "Current":
            I = t.Current_A
            total_i += I
            if t.Distribution == "Equal per item":
                groups = [([f], a, I / len(faces)) for f, a in zip(faces, areas)]
            else:
                groups = [(faces, sum(areas), I)]
            for k, (fl, area, ii) in enumerate(groups):
                j = ii / (area * 1e-6)
                c = _make_cd(doc, bc_name + ("_%d" % k if len(groups) > 1 else ""))
                c.References = [(fem, fl)]
                _set_cd(c, j)
                c.Label = "%s  I=%.4g A  J=%.4g A/m2" % (t.Label + ("[%d]" % k if len(groups) > 1 else ""),
                                                         ii, j)
                _add_prop(c, "App::PropertyBool", "PcbGenerated", "PCB", "created by terminal tool", True)
                an.addObject(c)
                tr["constraints"].append({"name": c.Name, "faces": fl, "current_A": ii,
                                          "area_mm2": round(area, 6), "J_A_per_m2": j})
            tr["current_A"] = I
        else:
            v = t.Potential_V if t.TermType == "Voltage" else 0.0
            c = _make_pot(doc, bc_name)
            c.References = [(fem, faces)]
            _set_pot(c, v)
            c.Label = "%s  V=%.4g V" % (t.Label, v)
            _add_prop(c, "App::PropertyBool", "PcbGenerated", "PCB", "created by terminal tool", True)
            an.addObject(c)
            tr["potential_V"] = v
            tr["constraints"].append({"name": c.Name, "faces": faces})
        report["terminals"].append(tr)
    floating = solids_with_cur - solids_with_pot
    if floating:
        report["warnings"].append("Current terminal(s) sit on copper island(s) %s that have no Ground/"
                                  "Voltage terminal: those islands are floating." % sorted(floating))
    if len(fused.Solids) > 1:
        report["warnings"].append("%d separate copper solids in the FEM shape; only islands with "
                                  "terminals carry current." % len(fused.Solids))
    report["sum_current_A"] = total_i
    _add_prop(an, "App::PropertyString", "PcbReport", "PCB", "last terminal report (JSON)",
              json.dumps(report))
    doc.recompute()

    log("FEM model for %s: %s (%d solid(s))" % (net, fem.Label, len(fused.Solids)))
    for tr in report["terminals"]:
        if tr["type"] == "Current":
            for cst in tr["constraints"]:
                log("  %-12s Current %+.4g A over %d face(s), A=%.5f mm2 -> J=%.6g A/m2"
                    % (tr["name"], cst["current_A"], len(cst["faces"]), cst["area_mm2"], cst["J_A_per_m2"]))
        else:
            log("  %-12s %s %.4g V on %d face(s), A=%.5f mm2"
                % (tr["name"], tr["type"], tr["potential_V"], len(tr["faces"]), tr["area_mm2"]))
    if abs(total_i) > 0:
        log("  Net current injected by Current terminals: %+.4g A (returns through Ground/Voltage)"
            % total_i)
    for w in report["warnings"]:
        log("  WARNING: " + w)

    if mesh_now:
        from femmesh.gmshtools import GmshTools
        err = GmshTools(mesh).create_mesh()
        doc.recompute()
        log("  mesh: %d nodes, %d volumes %s" % (mesh.FemMesh.NodeCount, mesh.FemMesh.VolumeCount,
                                                  "" if not err else "(gmsh: %s)" % err))
    from .solve import set_active, show_only_results
    set_active(an)                 # FreeCAD's own FEM commands now act on this analysis
    show_only_results(an)          # everything hidden except an existing solver result
    if mesh.FemMesh.NodeCount:
        prepare_mesh_for_elmer(an, log=log)
        if not mesh_now:
            log("  NOTE: mesh was not regenerated - if the copper or terminals changed, remesh and "
                "then use 'Prepare mesh for Elmer' before solving.")
    return report


def prepare_mesh_for_elmer(analysis, log=print) -> int:
    """
    FreeCAD's Gmsh tool names EVERY vertex/edge/face/solid of the shape as a mesh group.
    ElmerGrid supports at most 4000 named boundaries (MAXBCS) and 1000 bodies; beyond that
    body numbering breaks, ElmerSolver finds no elements for 'Solid1' ("No matrix exists")
    and FreeCAD shows an all-zero result. Keep only Solid groups and faces referenced by
    the analysis' constraints/materials/equations (unnamed boundaries = insulated, which is
    the correct natural BC for copper). Run after every remesh. Returns groups removed.
    """
    mesh = None
    keep = set()
    for m in analysis.Group:
        if m.TypeId == "Fem::FemMeshShapeBaseObjectPython" or m.TypeId.startswith("Fem::FemMesh"):
            mesh = mesh or m
        for r in (getattr(m, "References", None) or []):
            subs = r[1] if isinstance(r[1], (list, tuple)) else [r[1]]
            keep.update(subs)
    solver_eqs = [e for m in analysis.Group for e in getattr(m, "Group", [])
                  if hasattr(e, "References")]
    for e in solver_eqs:
        for r in (e.References or []):
            subs = r[1] if isinstance(r[1], (list, tuple)) else [r[1]]
            keep.update(subs)
    if mesh is None or not mesh.FemMesh.NodeCount:
        return 0
    fm = mesh.FemMesh.copy()
    removed = 0
    auto = re.compile(r"^(Vertex|Edge|Face)\d+$")
    for g in list(fm.Groups):
        nm = fm.getGroupName(g)
        if nm in keep or not auto.match(nm):      # Solid*, referenced faces, user mesh groups
            continue
        fm.removeGroup(g)
        removed += 1
    if removed:
        mesh.FemMesh = fm
    missing = sorted(k for k in keep if k.startswith("Face")
                     and k not in {fm.getGroupName(g) for g in fm.Groups})
    log("  mesh prepared for Elmer: removed %d unused groups, kept %d%s"
        % (removed, len(fm.Groups), "" if not missing else
           "; MISSING face groups %s - remesh!" % missing[:5]))
    return removed


def _read_vtu_ascii(path):
    """Minimal reader for Elmer's ASCII .vtu: returns (points[N][3], {name: list})."""
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    piece = root.find(".//Piece")
    pts_el = piece.find("./Points/DataArray")
    if (pts_el.get("format") or pts_el.get("FORMAT", "")).lower() != "ascii":
        raise ValueError("Binary VTU not supported - set the Elmer solver's BinaryOutput to False")
    vals = [float(v) for v in pts_el.text.split()]
    pts = [vals[i:i + 3] for i in range(0, len(vals), 3)]
    fields = {}
    for da in piece.findall("./PointData/DataArray"):
        n = int(da.get("NumberOfComponents", "1"))
        v = [float(x) for x in da.text.split()]
        fields[da.get("Name")] = v if n == 1 else [v[i:i + n] for i in range(0, len(v), n)]
    return pts, fields


def find_result_vtu(doc, net_obj) -> Optional[str]:
    """Newest .vtu in the Elmer working directory of the net's analysis."""
    import glob
    for o in doc.Objects:
        if o.TypeId == "Fem::FemAnalysis" and getattr(o, "PcbNet", None) == net_obj.NetName:
            for m in o.Group:
                wd = getattr(m, "WorkingDirectory", "")
                if wd and os.path.isdir(wd):
                    f = glob.glob(os.path.join(wd, "**", "*.vtu"), recursive=True)
                    if f:
                        return max(f, key=os.path.getmtime)
    return None


def terminal_report(doc, net_obj, vtu: Optional[str] = None, log=print) -> dict:
    """
    Read an Elmer static-current result and report each terminal's potential (mean/min/max
    over its contact face), the effective resistance of every current terminal to 0 V and
    the dissipated power P = sum(V_i * I_i) over current terminals (exact when all potential
    terminals are 0 V).
    """
    vtu = vtu or find_result_vtu(doc, net_obj)
    if not vtu or not os.path.isfile(vtu):
        raise FileNotFoundError("No Elmer result (.vtu) found - run the solver first or pass vtu=")
    pts, fields = _read_vtu_ascii(vtu)
    pot = fields.get("potential") or fields.get("Potential")
    if pot is None:
        raise ValueError("Result has no 'potential' field")
    if not any(abs(v) > 0 for v in pot):
        raise RuntimeError(
            "The result is all zeros: Elmer failed. Open the solver output (SolverElmer...Output) "
            "and look for 'Degenerate 3D element' (bad mesh) or 'No matrix exists' (mesh groups: "
            "use 'Prepare mesh for Elmer' after remeshing).")
    an = [o for o in doc.Objects if o.TypeId == "Fem::FemAnalysis"
          and getattr(o, "PcbNet", None) == net_obj.NetName][0]
    rep = json.loads(an.PcbReport)
    fem = doc.getObject(rep["fem_object"]).Shape
    # Elmer writes coordinates in mesh units (mm); guard against metre output
    span = max(abs(p[0]) for p in pts) or 1.0
    k = 1000.0 if span < 2.0 and fem.BoundBox.XLength > 2.0 else 1.0
    out = {"vtu": vtu, "terminals": [], "power_W": 0.0}
    has_nonzero_v = False
    log("Terminal results (%s):" % os.path.basename(vtu))
    for t in rep["terminals"]:
        vals = []
        for fn in t["faces"]:
            bb = fem.getElement(fn).BoundBox
            e = 1e-6 + 1e-6 * max(bb.XLength, bb.YLength)
            for p, v in zip(pts, pot):
                x, y, z = p[0] * k, p[1] * k, p[2] * k
                if (abs(z - bb.ZMin) < 1e-5 and bb.XMin - e <= x <= bb.XMax + e
                        and bb.YMin - e <= y <= bb.YMax + e):
                    vals.append(v)
        if not vals:
            log("  %-12s no result nodes on its faces" % t["name"])
            continue
        vm = sum(vals) / len(vals)
        r = {"name": t["name"], "type": t["type"], "V_mean": vm, "V_min": min(vals),
             "V_max": max(vals), "nodes": len(vals)}
        if t["type"] == "Current":
            r["current_A"] = t["current_A"]
            out["power_W"] += vm * t["current_A"]
            if t["current_A"]:
                r["R_to_0V_ohm"] = vm / t["current_A"]
            log("  %-12s I=%+.4g A  V=%.6g V (min %.6g, max %.6g)  V/I=%.6g mOhm"
                % (t["name"], t["current_A"], vm, r["V_min"], r["V_max"], 1e3 * vm / t["current_A"]
                   if t["current_A"] else float("nan")))
        else:
            if abs(t.get("potential_V", 0.0)) > 0:
                has_nonzero_v = True
            log("  %-12s %s V=%.6g V" % (t["name"], t["type"], vm))
        out["terminals"].append(r)
    if has_nonzero_v:
        out["power_W"] = None
        log("  (power not computed: a Voltage terminal with V != 0 carries an unknown current)")
    else:
        log("  Dissipated power sum(V*I) = %.6g W" % out["power_W"])
    return out


def apply_terminal_spec(doc, spec: dict, log=print) -> dict:
    """
    Headless/batch setup from a dict (or JSON file) like:
      {"net": "/VBUS",
       "mesh": {"max": 0.3, "min": 0.005, "curvature": 12, "now": false},
       "terminals": [
          {"name": "VIN", "type": "Current", "value": 2.0, "items": ["U1.1"],
           "distribution": "Uniform J", "scale": 0.8, "height": 0.05, "fill_holes": true},
          {"name": "RTN", "type": "Ground", "items": ["J1.1@bottom"]}]}
    """
    net = spec["net"]
    objs = [o for o in net_objects(doc) if o.NetName == net]
    if not objs:
        raise ValueError("Net %s not in document (import it first)" % net)
    net_obj = objs[0]
    existing = {t.Label: t for t in terminals_for(doc, net)}
    for td in spec["terminals"]:
        create_terminal(doc, net_obj, td["name"], td["items"], td.get("type", "Current"),
                        td.get("value", 0.0), td.get("distribution", "Uniform J"),
                        td.get("scale"), td.get("height"), td.get("fill_holes"),
                        obj=existing.get(td["name"]))
    m = spec.get("mesh", {})
    return build_fem_model(doc, net_obj, m.get("max"), m.get("min"), m.get("curvature"),
                           spec.get("joule_heating"), m.get("now"), log=log,
                           linear_solver=spec.get("linear_solver"),
                           algorithm3d=m.get("algorithm3d"), drop_islands=spec.get("drop_islands"))


