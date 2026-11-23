# SPDX-License-Identifier: LGPL-2.1-or-later
"""Import selected KiCad nets as FreeCAD bodies (one PartDesign::Body per net)."""
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

from . import __version__
from .kicad import Board, Options
from .shapes import ShapeBuilder, _fc, _safe_name, _PALETTE

def convert(pcb_path: str, nets: Sequence[str], out: Optional[str] = None, doc=None,
            opts: Optional[Options] = None, step_dir: Optional[str] = None,
            log=print) -> dict:
    """Convert `nets` (exact names or fnmatch patterns) of `pcb_path` into FreeCAD bodies."""
    opts = opts or Options()
    App, Part = _fc()
    board = Board(pcb_path, opts)
    for w in board.warnings:
        log("WARNING: " + w)
    selected = board.select_nets(nets)
    if not selected:
        raise ValueError("None of the requested nets were found. Use --list-nets.")
    log(board.stackup.table())
    log("Exporting %d net(s): %s" % (len(selected), ", ".join(selected)))
    geo = board.extract(selected)
    sb = ShapeBuilder(opts)

    if doc is None:
        doc = App.newDocument(_safe_name(os.path.splitext(os.path.basename(pcb_path))[0]))
    grp = doc.addObject("App::DocumentObjectGroup", "PCB_Nets")
    grp.Label = "PCB nets: " + os.path.basename(pcb_path)

    summary = {"board": os.path.abspath(pcb_path), "version": __version__,
               "options": {k: v for k, v in asdict(opts).items()},
               "stackup": [asdict(l) for l in board.stackup.layers],
               "dielectrics": board.stackup.dielectrics,
               "stackup_from_file": board.stackup.from_file, "nets": []}
    for i, net in enumerate(selected):
        g = geo[net]
        for w in g.warnings:
            log("WARNING [%s]: %s" % (net, w))
        log("  building %-24s %s" % (net, dict(g.counts)))
        shape, rep = sb.build_net(g, board.stackup)
        rep["counts"] = dict(g.counts)
        if shape is None:
            log("    (no copper for this net)")
            summary["nets"].append(rep)
            continue
        if rep.get("solids", 1) > 1:
            log("    note: %d disconnected copper islands in this net" % rep["solids"])
        nm = _safe_name(net)
        if opts.as_body:
            base = doc.addObject("Part::Feature", nm + "_copper")
            base.Shape = shape
            obj = doc.addObject("PartDesign::Body", nm)
            obj.BaseFeature = base
            if getattr(obj, "Tip", None) is None:
                try:
                    obj.Tip = base
                except Exception:
                    pass
        else:
            obj = doc.addObject("Part::Feature", nm)
            obj.Shape = shape
        obj.Label = net
        for pn, pv, doc_s in (("NetName", net, "KiCad net name"),
                              ("SourceBoard", os.path.abspath(pcb_path), "Source .kicad_pcb"),
                              ("PcbOptions", json.dumps(asdict(opts)), "Converter options (JSON)")):
            try:
                obj.addProperty("App::PropertyString", pn, "PCB", doc_s)
                setattr(obj, pn, pv)
            except Exception:
                pass
        grp.addObject(obj)
        if App.GuiUp:
            try:
                obj.ViewObject.ShapeColor = _PALETTE[i % len(_PALETTE)]
                if opts.as_body:
                    base.ViewObject.Visibility = False
            except Exception:
                pass
        if step_dir:
            os.makedirs(step_dir, exist_ok=True)
            p = os.path.join(step_dir, nm + ".step")
            shape.exportStep(p)
            rep["step"] = p
        summary["nets"].append(rep)
        log("    -> %s: %d solid(s), V=%.5f mm^3, valid=%s"
            % (net, rep.get("solids", 0), rep.get("volume_mm3", 0), rep.get("valid")))

    doc.recompute()
    if out:
        doc.saveAs(os.path.abspath(out))
        with open(os.path.splitext(out)[0] + "_fem.json", "w") as f:
            json.dump(summary, f, indent=2)
        log("Saved %s (+ _fem.json metadata)" % out)
    return summary


# ----------------------------------------------------------------------------------
# Reporting without FreeCAD
# ----------------------------------------------------------------------------------


def dry_run(pcb_path, nets, opts, log=print):
    b = Board(pcb_path, opts)
    for w in b.warnings:
        log("WARNING: " + w)
    log(b.stackup.table())
    sel = b.select_nets(nets) if nets else b.net_names()
    geo = b.extract(sel)
    for n in sel:
        g = geo[n]
        per_layer = {l: len(p) for l, p in g.layers.items() if p}
        log("%-28s %s  layers=%s" % (n, dict(g.counts), per_layer))
        for w in g.warnings:
            log("   WARNING: " + w)
    return b, geo


