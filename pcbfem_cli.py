#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""
PCB FEM command line. Run it with FreeCAD's Python (or any Python for --list-nets / --dry-run):

  "C:\\Program Files\\FreeCAD 1.0\\bin\\python.exe" pcbfem_cli.py board.kicad_pcb --nets GND -o gnd.FCStd
  ... --terminals spec.json        (terminals + Elmer analysis, see README)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _bootstrap():
    argv = sys.argv[1:]
    fc_path = None
    if "--freecad-path" in argv:
        fc_path = argv[argv.index("--freecad-path") + 1]
        sys.path.insert(0, fc_path)
    light = "--list-nets" in argv or "--dry-run" in argv
    try:
        import FreeCAD  # noqa: F401
    except ImportError:
        if not light:
            sys.path.insert(0, os.path.join(HERE, "freecad"))
            from pcbfem.cli import _autodetect_freecad
            _autodetect_freecad()
            sys.path.pop(0)
    # make this checkout importable as freecad.pcbfem (the name documents store for objects)
    try:
        import freecad
        if os.path.join(HERE, "freecad") not in list(freecad.__path__):
            freecad.__path__.append(os.path.join(HERE, "freecad"))
    except ImportError:
        import types
        freecad = types.ModuleType("freecad")
        freecad.__path__ = [os.path.join(HERE, "freecad")]
        sys.modules["freecad"] = freecad
    from freecad.pcbfem.cli import main
    return main(argv)


if __name__ == "__main__":
    sys.exit(_bootstrap())
