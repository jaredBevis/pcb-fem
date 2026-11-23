# SPDX-License-Identifier: LGPL-2.1-or-later
"""Command line interface (also usable without the GUI)."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .kicad import Board
from .importer import convert, dry_run
from . import prefs

# ----------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description="KiCad .kicad_pcb nets -> FreeCAD copper solids for FEM")
    ap.add_argument("pcb")
    ap.add_argument("--nets", nargs="*", default=[], help="net names or wildcard patterns")
    ap.add_argument("-o", "--out", help="output .FCStd (default: <board>_fem.FCStd)")
    ap.add_argument("--step", action="store_true", help="also export one STEP per net")
    ap.add_argument("--list-nets", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="parse and report only (no FreeCAD)")
    ap.add_argument("--plating", type=float, default=None, help="barrel wall, mm (default: preference, 0.025)")
    ap.add_argument("--hollow-microvias", action="store_true")
    ap.add_argument("--no-zones", action="store_true")
    ap.add_argument("--no-tracks", action="store_true")
    ap.add_argument("--parts", action="store_true", help="Part::Feature instead of PartDesign::Body")
    ap.add_argument("--copper", action="append", default=[], metavar="LAYER=MM",
                    help="override copper thickness, e.g. --copper F.Cu=0.07")
    ap.add_argument("--terminals", metavar="SPEC.json",
                    help="FEM terminal spec (see apply_terminal_spec); builds an Elmer analysis")
    ap.add_argument("--freecad-path", help="folder containing FreeCAD.pyd/.so (bin or lib)")
    a = ap.parse_args(argv)

    if a.freecad_path:
        sys.path.insert(0, a.freecad_path)
    elif not (a.list_nets or a.dry_run):
        _autodetect_freecad()
    o = prefs.options()
    o.plating = a.plating if a.plating is not None else o.plating
    o.microvia_solid = o.microvia_solid and not a.hollow_microvias
    o.include_zones = not a.no_zones
    o.include_tracks = not a.no_tracks
    o.as_body = o.as_body and not a.parts
    for c in a.copper:
        k, v = c.split("=")
        o.copper_override[k] = float(v)

    if a.list_nets:
        for n in Board(a.pcb, o).net_names():
            print(n)
        return 0
    if a.dry_run:
        dry_run(a.pcb, a.nets, o)
        return 0
    spec = None
    if a.terminals:
        with open(a.terminals) as f:
            spec = json.load(f)
        if spec["net"] not in a.nets:
            a.nets.append(spec["net"])
    if not a.nets:
        ap.error("--nets is required (use --list-nets to see them)")
    out = a.out or os.path.splitext(a.pcb)[0] + "_fem.FCStd"
    step_dir = os.path.join(os.path.dirname(os.path.abspath(out)), "fem_step") if a.step else None
    convert(a.pcb, a.nets, out=out if not spec else None, opts=o, step_dir=step_dir)
    if spec:
        import FreeCAD as App
        from .fem import apply_terminal_spec
        doc = App.ActiveDocument
        rep = apply_terminal_spec(doc, spec)
        doc.saveAs(os.path.abspath(out))
        with open(os.path.splitext(out)[0] + "_terminals.json", "w") as f:
            json.dump(rep, f, indent=2)
        print("Saved %s" % out)
    return 0


def _autodetect_freecad():
    """Put a FreeCAD install on sys.path if `import FreeCAD` would otherwise fail."""
    try:
        import FreeCAD  # noqa
        return
    except ImportError:
        pass
    import glob
    cands = []
    if os.name == "nt":
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("LOCALAPPDATA", "")):
            cands += sorted(glob.glob(os.path.join(root, "FreeCAD*", "bin")), reverse=True)
            cands += sorted(glob.glob(os.path.join(root, "Programs", "FreeCAD*", "bin")), reverse=True)
    else:
        cands += ["/usr/lib/freecad/lib", "/usr/lib/freecad-python3/lib", "/usr/local/lib/freecad/lib",
                  "/Applications/FreeCAD.app/Contents/Resources/lib"]
    for c in cands:
        if os.path.isdir(c):
            sys.path.insert(0, c)
            if os.name == "nt" and hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(c)
                except OSError:
                    pass
            try:
                import FreeCAD  # noqa: F401,F811
                return
            except ImportError:
                sys.path.remove(c)
